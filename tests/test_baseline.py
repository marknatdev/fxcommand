"""T7: the Coin-Flip Baseline matches the strategy's counts, hours and sides, without bias."""

from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import numpy as np
import pytest
import test_walkforward as twf

from vibedesk.blocks.spec import expand_grid, validate
from vibedesk.engine.simulate import simulate_prepared
from vibedesk.gauntlet.baseline import (
    _server_hours,
    coin_flip_baseline,
    draw_year,
    entry_signals,
    fold_plans,
)
from vibedesk.gauntlet.walkforward import walk_forward
from vibedesk.market.bars import Timeframe
from vibedesk.market.synthetic import SyntheticBarSource

# A long-holding Spec: on synthetic H1 it is in the market about 95% of the time.
LONG_HOLDING = {
    "name": "long-holding",
    "symbol": "GOLD",
    "timeframe": "H1",
    "direction": "both",
    "entry": {"block": "donchian_breakout", "params": {"period": 20}},
    "exit": {
        "stop": {"block": "atr_stop", "params": {"mult": 3.0}},
        "trailing": {"block": "atr_trailing", "params": {"mult": 6.0}},
    },
    "sizing": {"block": "fixed_risk", "params": {}},
}


def walk(seed, spec=twf.SPEC, min_trades=30):
    source = SyntheticBarSource(seed, date(2014, 1, 1), date(2019, 1, 1))
    bars, m1 = source.fetch("GOLD", Timeframe.H1), source.fetch("GOLD", Timeframe.M1)
    combinations = expand_grid(validate(spec))
    return walk_forward(combinations, bars, twf.COST, m1, years=twf.YEARS, min_trades=min_trades)


@pytest.fixture(scope="module")
def wf():
    return walk(3)


@pytest.fixture(scope="module")
def long_holding():
    return walk(2, LONG_HOLDING, min_trades=10)


def test_every_run_draws_exactly_the_strategys_count_each_year(wf):
    baseline = coin_flip_baseline(wf, seed=7, runs=200)

    assert baseline.fold_trades.sum() == len(wf.trades) > 50
    assert (baseline.run_fold_trades == baseline.fold_trades).all()


def test_hours_and_sides_match_the_strategy_exactly(wf):
    runs = 1000
    hours = _server_hours(wf.market.time)[wf.trades["entry_idx"]]

    baseline = coin_flip_baseline(wf, seed=1, runs=runs)

    # Each random entry keeps the server hour of the strategy entry it stands for, and the
    # sides are a permutation of the strategy's: the mixes match exactly, not just within 2%.
    assert baseline.hour_counts.tolist() == (runs * np.bincount(hours, minlength=24)).tolist()
    assert baseline.long_trades == runs * int((wf.trades["side"] == 1).sum())


def test_a_year_draws_distinct_bars_of_the_right_hours(wf):
    rng = np.random.default_rng(0)
    hour = _server_hours(wf.market.time)

    for plan in fold_plans(wf):
        records = draw_year(plan, rng)
        assert len(np.unique(records["entry_idx"])) == len(records) == len(plan.hours)
        assert sorted(hour[records["entry_idx"]]) == sorted(plan.hours)
        assert np.all(np.diff(records["entry_idx"]) > 0)


@pytest.mark.parametrize("side", [1, -1])
def test_every_candidate_trade_is_what_the_engine_trades_for_that_entry_alone(wf, side):
    rng = np.random.default_rng(side + 5)
    row = 0 if side == 1 else 1
    for plan in fold_plans(wf):
        for c in rng.choice(len(plan.candidates), size=25, replace=False):
            bar = plan.candidates[c : c + 1]
            alone = entry_signals(plan.signals, bar, np.array([side]), len(wf.market))

            [record] = simulate_prepared(wf.market, alone).records

            assert record.tobytes() == plan.trades[row, c].tobytes()


def mean_bias(wf, draws=1000):
    """Mean random trade minus the mean hour- and side-matched trade, over many years drawn."""
    rng = np.random.default_rng(1)
    drawn, expected = [], []
    for plan in fold_plans(wf):
        long_share = float((plan.sides == 1).mean())
        for g in plan.groups:
            members = plan.group_members[plan.group_offsets[g] : plan.group_offsets[g + 1]]
            r = plan.trades["r"][:, members].mean(axis=1)
            expected.append(long_share * r[0] + (1 - long_share) * r[1])
        drawn += [draw_year(plan, rng)["r"] for _ in range(draws)]
    return np.concatenate(drawn).mean() - np.mean(expected)


@pytest.mark.parametrize("case", ["wf", "long_holding"])
def test_random_trades_are_not_biased_by_their_own_length(case, request):
    # Fitting random trades one at a time between each other only lets short trades in when
    # the strategy is in the market most of the time, and short trades are mostly stop-outs:
    # that rule missed by 0.093R a trade on the long-holding data (94% in the market). Drawn
    # independently, the gap settles within 0.003R.
    wf = request.getfixturevalue(case)

    assert mean_bias(wf) == pytest.approx(0.0, abs=0.03)


def test_a_long_holding_spec_is_really_in_the_market_most_of_the_time(long_holding):
    t = long_holding.trades.records
    held = (t["exit_idx"] - t["entry_idx"] + 1).sum()

    assert held / len(long_holding.blind_bars) > 0.8


def test_the_same_seed_gives_the_same_distribution(wf):
    a = coin_flip_baseline(wf, seed=11, runs=50)
    b = coin_flip_baseline(wf, seed=11, runs=50)
    c = coin_flip_baseline(wf, seed=12, runs=50)

    assert a.net_r.tolist() == b.net_r.tolist()
    assert a.net_r.tolist() != c.net_r.tolist()


def test_percentile_and_p95(wf):
    baseline = coin_flip_baseline(wf, seed=2, runs=200)

    below = np.mean(baseline.net_r < wf.metrics.net_r)
    ties = np.mean(baseline.net_r == wf.metrics.net_r)
    assert baseline.strategy_net_r == wf.metrics.net_r
    assert baseline.percentile == pytest.approx(below + ties / 2)
    assert baseline.p95 == pytest.approx(np.percentile(baseline.net_r, 95))


def test_a_run_that_ties_the_strategy_counts_half(wf):
    # Random runs rarely tie a real net R, so the strategy is set to the 100th smallest of 200
    # distinct runs: 99 runs below and 1 tie give (99 + 0.5) / 200 = 0.4975. Counting the tie
    # as beaten would give 100 / 200 = 0.5. The same seed draws the same runs again.
    runs = coin_flip_baseline(wf, seed=2, runs=200).net_r
    assert len(np.unique(runs)) == 200
    tied = replace(wf, metrics=SimpleNamespace(net_r=float(np.sort(runs)[99])))

    baseline = coin_flip_baseline(tied, seed=2, runs=200)

    assert baseline.net_r.tolist() == runs.tolist()
    assert baseline.percentile == pytest.approx(0.4975, abs=1e-12)


def test_years_without_strategy_trades_draw_nothing(wf):
    plans = fold_plans(wf)
    traded = {int(f) for f in np.unique(wf.trade_fold)}

    assert {plan.fold for plan in plans} == traded


@pytest.mark.slow
def test_a_strategy_with_no_edge_lands_anywhere_in_its_baseline():
    # On a random walk a breakout strategy has no edge, so its percentile against its own
    # baseline should be spread over 0..1, not piled at the top (a lenient baseline) or the
    # bottom. Thirty seeds; thresholds fixed before looking at the result.
    percentiles = []
    for seed in range(30):
        wf = walk(100 + seed)
        percentiles.append(coin_flip_baseline(wf, seed=seed, runs=200).percentile)
    percentiles = np.array(percentiles)

    assert 0.3 <= percentiles.mean() <= 0.7
    assert (percentiles >= 0.95).sum() <= 5
