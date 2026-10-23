"""T9: a Trial end to end, its stored record, the Trial Budget and the Holdout."""

import json
import sqlite3
from datetime import UTC, date, datetime

import numpy as np
import pytest

from vibedesk.blocks.spec import expand_grid, validate
from vibedesk.engine.costs import CostSnapshot
from vibedesk.gauntlet.trial import (
    HOLDOUT_FAIL,
    PASS,
    HoldoutRefused,
    expected_holdout_trades,
    request_holdout,
    run_holdout,
    run_trial,
)
from vibedesk.gauntlet.verdict import CANDIDATE, REJECT
from vibedesk.ledger import BudgetExhausted, HoldoutSpent, Ledger
from vibedesk.ledger.store import MIGRATIONS
from vibedesk.market.bars import Bars, Timeframe
from vibedesk.market.data import HoldoutLocked, MarketData
from vibedesk.market.grant import HOLDOUT_START
from vibedesk.market.synthetic import (
    PLANTED_EDGE,
    PLANTED_EDGE_RANGE,
    SyntheticBarSource,
    SyntheticConfig,
)
from vibedesk.rules import RULES_VERSION

YEARS = (2023, 2024)
NOW = datetime(2026, 10, 1, tzinfo=UTC)
COST = CostSnapshot(0.01, 17, 1, swap_long_points=-83.83, swap_short_points=20.64)  # T10 floor
SPEC = {
    "name": "london-breakout",
    "symbol": "GOLD",
    "timeframe": "M15",
    "direction": "both",
    "entry": {"block": "range_breakout", "params": {"session": "london_open", "range_bars": 4}},
    "exit": {
        "stop": {"block": "atr_stop", "params": {"mult": 1.5}},
        "target": {"block": "r_target", "params": {"r": 2.0}},
    },
    "sizing": {"block": "fixed_risk", "params": {}},
}


class RecordingSource:
    """A BarSource that remembers every range it was asked for."""

    def __init__(self, source):
        self.source = source
        self.fetches = []

    def fetch(self, symbol, timeframe, start=None, end=None):
        self.fetches.append((symbol, timeframe, start, end))
        return self.source.fetch(symbol, timeframe, start, end)


# The Trials train from 2020 and the Holdout runs to 2026-10, so the data starts in 2020: a
# shorter range than the planted edge's calibration, which keeps the default suite fast.
RANGE = (date(2020, 1, 1), PLANTED_EDGE_RANGE[1])


@pytest.fixture(scope="module")
def planted():
    return SyntheticBarSource(1, *RANGE, config=PLANTED_EDGE)


@pytest.fixture(scope="module")
def walk():
    return SyntheticBarSource(1, *RANGE, config=SyntheticConfig())


def desk(path, source):
    ledger = Ledger(path)
    recording = RecordingSource(source)
    family = ledger.create_family("London breakout")
    hypothesis = ledger.register_hypothesis(family, SPEC)
    return ledger, MarketData(recording, ledger), recording, hypothesis


def trial(ledger, market, hypothesis, **kwargs):
    return run_trial(ledger, market, hypothesis, COST, years=YEARS, runs=200, **kwargs)


def strict(text):
    def refuse(token):
        raise ValueError(f"{token} is not JSON")

    return json.loads(text, parse_constant=refuse)


@pytest.fixture(scope="module")
def candidate(planted, tmp_path_factory):
    ledger, market, recording, hypothesis = desk(tmp_path_factory.mktemp("c") / "desk.db", planted)
    first = trial(ledger, market, hypothesis, use_m1=True)
    second = trial(ledger, market, hypothesis, use_m1=False)
    return ledger, market, recording, hypothesis, first, second


def test_a_trial_adds_exactly_one_row_with_its_verdict(candidate):
    ledger, _, _, hypothesis, first, second = candidate
    family = ledger.hypothesis(hypothesis).family_id

    assert ledger.trial_count(family) == 2
    assert [t.id for t in ledger.trials_for(hypothesis)] == [first.trial_id, second.trial_id]
    assert first.verdict.verdict == CANDIDATE
    assert ledger.trial(first.trial_id).verdict == CANDIDATE
    assert ledger.trial(first.trial_id).closest_miss is None


def test_the_stored_record_is_strict_json_and_what_the_trial_returned(candidate):
    ledger, _, _, _, first, _ = candidate

    stored = strict(ledger.trial(first.trial_id).result_json)

    assert stored == first.record


def test_every_verdict_card_figure_can_be_rebuilt_from_the_store(candidate):
    ledger, _, _, _, first, _ = candidate
    row = ledger.trial(first.trial_id)
    record = strict(row.result_json)
    wf = record["walk_forward"]
    m = wf["metrics"]

    # The equity curve, the folds and the trades all add up to the stitched net R.
    assert sum(wf["daily_r"]["r"]) == pytest.approx(m["net_r"])
    assert sum(wf["fold_net_r"]) == pytest.approx(m["net_r"])
    assert sum(wf["trade_list"]["r"]) == pytest.approx(m["net_r"])
    assert len(wf["trade_list"]["r"]) == m["trades"]
    # The baseline histogram and its marker.
    net = np.array(record["baseline"]["net_r"])
    assert len(net) == record["baseline"]["runs"] == 200
    assert record["baseline"]["p95"] == pytest.approx(np.percentile(net, 95))
    strategy = record["baseline"]["strategy_net_r"]
    assert record["baseline"]["percentile"] == pytest.approx(
        np.mean(net < strategy) + 0.5 * np.mean(net == strategy)
    )
    # The checks, each with its value and threshold, as the Verdict decided them.
    checks = {c["name"]: c for c in record["checks"]}
    assert checks["profit_factor"]["value"] == pytest.approx(m["profit_factor"])
    assert checks["deflated_sharpe"]["value"] == pytest.approx(record["deflated_sharpe"]["value"])
    assert all(c["status"] in ("pass", "n/a") for c in record["checks"])
    # The Ledger's own columns agree with the record.
    assert [label for label, _ in row.combinations] == wf["combinations"]
    assert [s for _, s in row.combinations] == pytest.approx(wf["blind_sharpe"])
    assert row.cost == COST
    assert record["verdict"] == row.verdict
    assert record["rules_version"] == row.rules_version == RULES_VERSION


def test_the_deflated_sharpe_counts_every_trial_of_the_family(candidate):
    _, _, _, _, first, second = candidate

    assert first.record["deflated_sharpe"]["n_trials"] == 1  # a grid of one, this Trial
    assert second.record["deflated_sharpe"]["n_trials"] == 2


def test_settling_on_m1_is_recorded_and_seeds_its_own_baseline(candidate):
    # M1 changes how same-bar conflicts settle, so it is part of what the Trial judged: it is
    # recorded with its fingerprint, and it changes the baseline's seed.
    _, _, _, _, first, second = candidate

    assert first.record["trial_number"] == 1 and second.record["trial_number"] == 2
    assert first.record["m1"]["used"] and first.record["m1"]["bars"] > 0
    assert len(first.record["m1"]["sha256"]) == 64
    assert second.record["m1"] == {"used": False, "bars": 0, "sha256": None}
    assert first.record["baseline"]["seed"] != second.record["baseline"]["seed"]


def test_an_unchanged_rerun_draws_the_same_baseline(planted, tmp_path):
    # The seed comes from what the Trial judges, not from its number, so re-running the same
    # Spec on the same bars can't re-roll the baseline's luck. The Deflated Sharpe gives such a
    # re-run nothing to deflate by either (identical Sharpes, so V[SR] = 0).
    ledger, market, _, hypothesis = desk(tmp_path / "desk.db", planted)

    first = trial(ledger, market, hypothesis, use_m1=False)
    again = trial(ledger, market, hypothesis, use_m1=False)

    assert again.record["trial_number"] == 2
    assert again.record["baseline"]["seed"] == first.record["baseline"]["seed"]
    assert again.record["baseline"]["net_r"] == first.record["baseline"]["net_r"]


def test_the_research_window_never_reaches_the_holdout(candidate):
    _, _, recording, _, _, _ = candidate

    research = recording.fetches[:3]  # two Trials: M15 + M1, then M15 only
    assert [f[1] for f in research] == [Timeframe.M15, Timeframe.M1, Timeframe.M15]
    assert all(end <= HOLDOUT_START for _, _, _, end in research)


def test_blind_years_inside_the_holdout_are_refused(planted, tmp_path):
    ledger, market, recording, hypothesis = desk(tmp_path / "desk.db", planted)

    with pytest.raises(ValueError, match="before the Holdout"):
        run_trial(ledger, market, hypothesis, COST, years=(2024, 2025))
    assert recording.fetches == []


def fill_budget(ledger, hypothesis):
    spec = ledger.hypothesis(hypothesis)
    grid = expand_grid(validate(spec.spec_json))
    n = 8
    bars = Bars("GOLD", Timeframe.M15, 0.01, 2, 1_420_070_400 + 900 * np.arange(n),
                *[np.full(n, 1200.0)] * 4, np.full(n, 30))  # fmt: skip
    for _ in range(ledger.budget(spec.family_id)):
        ledger.record_trial(
            hypothesis,
            combinations=[(c, 0.0) for c in grid],
            verdict=REJECT,
            closest_miss={"check": "profit_factor", "value": 0.5, "threshold": 1.2},
            cost=COST,
            bars=bars,
            rules_version=RULES_VERSION,
            result_json="{}",
        )


def test_a_trial_past_the_budget_is_refused_before_any_work(planted, tmp_path):
    ledger, market, recording, hypothesis = desk(tmp_path / "desk.db", planted)
    fill_budget(ledger, hypothesis)

    with pytest.raises(BudgetExhausted):
        trial(ledger, market, hypothesis)
    assert recording.fetches == []  # not one bar was fetched
    assert ledger.trial_count(ledger.hypothesis(hypothesis).family_id) == 25


def requests(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT COUNT(*) FROM holdout_requests").fetchone()[0]


def test_a_reject_cannot_ask_for_the_holdout(walk, tmp_path):
    path = tmp_path / "desk.db"
    ledger, market, _, hypothesis = desk(path, walk)
    result = trial(ledger, market, hypothesis, use_m1=False)

    with pytest.raises(HoldoutRefused) as refused:
        request_holdout(ledger, hypothesis, now=NOW)

    assert result.verdict.verdict == REJECT
    assert refused.value.reason == "NOT_CANDIDATE"
    assert requests(path) == 0


def test_too_few_expected_holdout_trades_spend_nothing(candidate, tmp_path):
    ledger, _, _, hypothesis, first, _ = candidate
    soon = datetime(2025, 1, 10, tzinfo=UTC)

    with pytest.raises(HoldoutRefused) as refused:
        request_holdout(ledger, hypothesis, now=soon)

    assert refused.value.reason == "INSUFFICIENT_TRADES"
    assert expected_holdout_trades(first.record, soon) < 30
    assert ledger.holdout_spend(ledger.hypothesis(hypothesis).family_id) is None


def test_the_holdout_is_spent_once(planted, tmp_path):
    path = tmp_path / "desk.db"
    ledger, market, recording, hypothesis = desk(path, planted)
    first = trial(ledger, market, hypothesis, use_m1=False)
    family = ledger.hypothesis(hypothesis).family_id

    request = request_holdout(ledger, hypothesis, now=NOW)
    grant = ledger.approve_holdout(request)
    holdout = run_holdout(ledger, market, grant, now=NOW, use_m1=False)

    # It traded the Holdout's own Combination over the Holdout, under the Trial's costs.
    assert first.verdict.verdict == CANDIDATE
    assert holdout.verdict == PASS
    assert len(holdout.trades) >= 30
    assert holdout.trades["entry_time"].min() >= HOLDOUT_START.timestamp()
    assert holdout.metrics.profit_factor >= 1.1
    assert recording.fetches[-1][3] == NOW  # this fetch, and only this one, reached past 2025
    spend = ledger.holdout_spend(family)
    assert (spend.verdict, strict(spend.result_json)) == (PASS, holdout.record)

    # The family has spent its Holdout: no new request, and the grant opens nothing.
    with pytest.raises(HoldoutSpent):
        request_holdout(ledger, hypothesis, now=NOW)
    with pytest.raises(HoldoutLocked):
        run_holdout(ledger, market, grant, now=NOW, use_m1=False)
    assert requests(path) == 1


def test_a_holdout_without_the_edge_fails(walk, tmp_path):
    # Force a random-walk Hypothesis to the Holdout through the Ledger, as if the Trial had
    # been a CANDIDATE: the Holdout judges on its own numbers.
    ledger, market, _, hypothesis = desk(tmp_path / "desk.db", walk)
    trial(ledger, market, hypothesis, use_m1=False)
    stored = ledger.trials_for(hypothesis)[-1]
    record = strict(stored.result_json)
    # The Holdout gate re-judges the stored daily R's Deflated Sharpe, so the forged CANDIDATE
    # carries a blind record that would pass it; the Holdout itself still sees no edge.
    days = len(record["walk_forward"]["daily_r"]["r"])
    record["walk_forward"]["daily_r"]["r"] = (
        0.5 + np.random.default_rng(0).normal(size=days)
    ).tolist()
    ledger.record_trial(
        hypothesis,
        combinations=list(
            zip(
                expand_grid(validate(ledger.hypothesis(hypothesis).spec_json)),
                [s for _, s in stored.combinations],
                strict=True,
            )
        ),
        verdict=CANDIDATE,
        closest_miss=None,
        cost=COST,
        bars=walk.fetch("GOLD", Timeframe.M15, end=HOLDOUT_START),
        rules_version=RULES_VERSION,
        result_json=json.dumps(record),
    )
    grant = ledger.approve_holdout(ledger.request_holdout(hypothesis))

    holdout = run_holdout(ledger, market, grant, now=NOW, use_m1=False)

    assert holdout.verdict == HOLDOUT_FAIL
    assert holdout.metrics.profit_factor < 1.1


def test_a_trial_that_turns_the_verdict_before_the_holdout_runs_stops_it(planted, tmp_path):
    # CANDIDATE, request, approval, then one more Trial that comes back REJECT: the Holdout
    # must not be spent on the stale CANDIDATE, and not one Holdout bar may be fetched.
    ledger, market, recording, hypothesis = desk(tmp_path / "desk.db", planted)
    trial(ledger, market, hypothesis, use_m1=False)
    grant = ledger.approve_holdout(request_holdout(ledger, hypothesis, now=NOW))
    spec = ledger.hypothesis(hypothesis)
    n = 8
    bars = Bars("GOLD", Timeframe.M15, 0.01, 2, 1_420_070_400 + 900 * np.arange(n),
                *[np.full(n, 1200.0)] * 4, np.full(n, 30))  # fmt: skip
    ledger.record_trial(
        hypothesis,
        combinations=[(c, 0.0) for c in expand_grid(validate(spec.spec_json))],
        verdict=REJECT,
        closest_miss={"check": "deflated_sharpe", "value": 0.9, "threshold": 0.95},
        cost=COST,
        bars=bars,
        rules_version=RULES_VERSION,
        result_json="{}",
    )
    fetched = len(recording.fetches)

    with pytest.raises(HoldoutRefused) as refused:
        run_holdout(ledger, market, grant, now=NOW, use_m1=False)

    assert refused.value.reason == "NOT_CANDIDATE"
    assert ledger.holdout_spend(spec.family_id) is None
    assert len(recording.fetches) == fetched


def test_a_rule_change_stops_the_holdout_request_and_the_holdout(planted, tmp_path, monkeypatch):
    # CANDIDATE, request and approval, then the trade rules change (RULES_VERSION goes up): the
    # Holdout would judge the Combination by other rules than its Trial. No new request is
    # recorded, the approved grant spends nothing, and not one Holdout bar is fetched.
    path = tmp_path / "desk.db"
    ledger, market, recording, hypothesis = desk(path, planted)
    trial(ledger, market, hypothesis, use_m1=False)
    grant = ledger.approve_holdout(request_holdout(ledger, hypothesis, now=NOW))
    monkeypatch.setattr("vibedesk.gauntlet.trial.RULES_VERSION", RULES_VERSION + 1)
    fetched = len(recording.fetches)

    with pytest.raises(HoldoutRefused) as asked:
        request_holdout(ledger, hypothesis, now=NOW)
    with pytest.raises(HoldoutRefused) as spent:
        run_holdout(ledger, market, grant, now=NOW, use_m1=False)

    assert asked.value.reason == spent.value.reason == "RULES_CHANGED"
    assert f"rules version {RULES_VERSION};" in str(spent.value)
    assert requests(path) == 1
    assert ledger.holdout_spend(ledger.hypothesis(hypothesis).family_id) is None
    assert len(recording.fetches) == fetched


def test_a_candidate_from_before_rules_versions_cannot_ask_for_the_holdout(candidate, tmp_path):
    # A CANDIDATE recorded by migration 1 alone has no rules version: nobody can say which
    # rules judged it, so it cannot ask for the Holdout.
    *_, first, _ = candidate
    path = tmp_path / "desk.db"
    old = sqlite3.connect(path)
    old.executescript(f"BEGIN;\n{MIGRATIONS[0]}\nPRAGMA user_version = 1;\nCOMMIT;")
    old.execute("INSERT INTO families (idea) VALUES ('London breakout')")
    old.execute(
        "INSERT INTO hypotheses (family_id, spec_json, entry_block, symbol, timeframe)"
        " VALUES (1, ?, 'range_breakout', 'GOLD', 'M15')",
        (validate(SPEC).model_dump_json(),),
    )
    old.execute(
        "INSERT INTO trials (hypothesis_id, verdict, cost_json, bars_count, bars_first,"
        " bars_last, bars_sha256, result_json) VALUES (1, 'CANDIDATE', ?, 1, 0, 0, '', ?)",
        (COST.to_json(), json.dumps(first.record)),
    )
    old.commit()
    old.close()

    with Ledger(path) as ledger, pytest.raises(HoldoutRefused) as refused:
        request_holdout(ledger, 1, now=NOW)

    assert refused.value.reason == "RULES_CHANGED"
    assert "before rules were versioned" in str(refused.value)
    assert requests(path) == 0


def daily_r_with_sharpe(sharpe, days, seed=0):
    """Daily R whose sample Sharpe is exactly `sharpe`."""
    z = np.random.default_rng(seed).normal(size=days)
    return (sharpe + (z - z.mean()) / z.std(ddof=1)).tolist()


def test_a_sibling_grid_can_turn_a_candidate_stale_before_the_holdout(planted, tmp_path):
    # A CANDIDATE is judged at N = 1: a daily Sharpe of 0.065 over 2,000 days has a Deflated
    # Sharpe of about Phi(0.065 x sqrt(1999)) = 0.998. Then a sibling Hypothesis of the same
    # family (same entry Block and symbol, ADR 0003) records 23 REJECTs of a 48-Combination grid:
    # N = 1 + 23 x 48 = 1105, and the luck a grid that wide finds by chance (SR0 about 0.07)
    # overtakes 0.065. The CANDIDATE never had a new Trial of its own, yet the Holdout is refused.
    path = tmp_path / "desk.db"
    ledger, market, recording, a = desk(path, planted)
    family = ledger.hypothesis(a).family_id
    eight = Bars("GOLD", Timeframe.M15, 0.01, 2, 1_420_070_400 + 900 * np.arange(8),
                 *[np.full(8, 1200.0)] * 4, np.full(8, 30))  # fmt: skip
    record = {
        "walk_forward": {"daily_r": {"r": daily_r_with_sharpe(0.065, 2000)}},
        "holdout": {"rate_per_year": 100.0},
    }
    [only] = expand_grid(validate(SPEC))
    ledger.record_trial(
        a, combinations=[(only, 0.065)], verdict=CANDIDATE, closest_miss=None, cost=COST,
        bars=eight, rules_version=RULES_VERSION, result_json=json.dumps(record),
    )  # fmt: skip
    grant = ledger.approve_holdout(request_holdout(ledger, a, now=NOW))  # fine at N = 1

    mults = [round(0.5 + 0.25 * k, 2) for k in range(48)]
    sibling = SPEC | {
        "exit": SPEC["exit"] | {"stop": {"block": "atr_stop", "params": {"mult": mults}}}
    }
    b = ledger.register_hypothesis(family, sibling)
    grid = expand_grid(validate(ledger.hypothesis(b).spec_json))
    rng = np.random.default_rng(1)
    for _ in range(23):
        sharpes = rng.normal(0.0, 0.0216, len(grid))
        ledger.record_trial(
            b, combinations=list(zip(grid, sharpes, strict=True)), verdict=REJECT,
            closest_miss={"check": "profit_factor", "value": 0.9, "threshold": 1.2}, cost=COST,
            bars=eight, rules_version=RULES_VERSION, result_json="{}",
        )  # fmt: skip
    fetched = len(recording.fetches)

    with pytest.raises(HoldoutRefused) as asked:
        request_holdout(ledger, a, now=NOW)
    with pytest.raises(HoldoutRefused) as spent:
        run_holdout(ledger, market, grant, now=NOW)

    assert ledger.deflation_inputs(family)[0] == 1105
    assert asked.value.reason == spent.value.reason == "DEFLATED_SHARPE_FELL"
    assert "N = 1105" in str(spent.value)
    assert requests(path) == 1
    assert ledger.holdout_spend(family) is None
    assert len(recording.fetches) == fetched
