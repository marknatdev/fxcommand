"""The Ledger (spec §6.4, §9.3; ADRs 0002, 0003): append-only, budgeted, one Holdout spend."""

import ast
import json
import re
import sqlite3
import statistics
from pathlib import Path

import numpy as np
import pytest

from vibedesk.blocks.spec import SpecInvalid, expand_grid, validate
from vibedesk.engine.costs import CostSnapshot
from vibedesk.ledger import (
    TRIAL_BUDGET,
    BarsFingerprint,
    BudgetExhausted,
    FamilyConflict,
    HoldoutRequest,
    HoldoutSpent,
    Ledger,
    LedgerError,
    NotFound,
)
from vibedesk.ledger.store import MIGRATIONS
from vibedesk.market.bars import Bars, Timeframe
from vibedesk.market.grant import HoldoutGrant
from vibedesk.rules import RULES_VERSION

SRC = Path(__file__).resolve().parents[1] / "src"

COST = CostSnapshot(
    0.01, 55, 1, swap_long_points=-83.83, swap_short_points=20.64, as_of="2026-10-21"
)
DONCHIAN = {"block": "donchian_breakout", "params": {"period": 20}}
MA_CROSS = {"block": "ma_cross", "params": {"fast": 10, "slow": 50, "type": "ema"}}
MISS = {"check": "oos_profit_factor", "value": 1.12, "threshold": 1.2}


def make_spec(symbol="GOLD", entry=DONCHIAN, mult=2.0, name="breakout"):
    return {
        "name": name,
        "symbol": symbol,
        "timeframe": "M15",
        "direction": "both",
        "entry": entry,
        "exit": {
            "stop": {"block": "atr_stop", "params": {"period": 14, "mult": mult}},
            "target": {"block": "r_target", "params": {"r": 2.0}},
        },
        "sizing": {"block": "fixed_risk", "params": {}},
    }


def make_bars(symbol="GOLD", n=8, shift=0.0):
    time = 1_420_070_400 + 900 * np.arange(n)
    close = 1200.0 + np.arange(n) + shift
    spread = np.full(n, 30)
    return Bars(symbol, Timeframe.M15, 0.01, 2, time, close, close + 1, close - 1, close, spread)


def record(ledger, hypothesis_id, sharpes=None, verdict="REJECT"):
    hypothesis = ledger.hypothesis(hypothesis_id)
    grid = expand_grid(validate(hypothesis.spec_json))
    sharpes = [0.0] * len(grid) if sharpes is None else sharpes
    return ledger.record_trial(
        hypothesis_id,
        combinations=list(zip(grid, sharpes, strict=True)),
        verdict=verdict,
        closest_miss=MISS if verdict == "REJECT" else None,
        cost=COST,
        bars=make_bars(hypothesis.symbol),
        rules_version=RULES_VERSION,
        result_json='{"folds": [], "checks": []}',
    )


@pytest.fixture
def path(tmp_path):
    return tmp_path / "desk.db"


@pytest.fixture
def ledger(path):
    with Ledger(path) as ledger:
        yield ledger


@pytest.fixture
def raw(path, ledger):
    """A second connection that skips the Ledger's code, as a stray writer would."""
    connection = sqlite3.connect(path)
    yield connection
    connection.close()


def approved(ledger, hypothesis_id):
    return ledger.approve_holdout(ledger.request_holdout(hypothesis_id))


# Append-only

TAMPERING = {
    "families": "idea = 'a different idea'",
    "hypotheses": "symbol = 'EURUSD'",
    "trials": "result_json = '{}'",
    "trial_combinations": "blind_sharpe = 9.9",
    "budget_grants": "amount = 1000",
    "holdout_requests": "hypothesis_id = hypothesis_id",
    "holdout_approvals": "token = 'forged'",
    "holdout_spends": "verdict = 'PASS'",
}


@pytest.fixture
def full_ledger(ledger):
    """A Ledger with at least one row in every table."""
    family = ledger.create_family("London breakout on gold")
    hypothesis = ledger.register_hypothesis(family, make_spec(mult=[1.5, 2.0]))
    record(ledger, hypothesis, sharpes=[0.1, 0.2])
    ledger.extend_budget(family)
    ledger.record_holdout(approved(ledger, hypothesis), "HOLDOUT FAIL", "{}")
    return ledger


def test_every_table_is_covered(full_ledger, raw):
    tables = {name for (name,) in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == set(TAMPERING)


@pytest.mark.parametrize("table", sorted(TAMPERING))
def test_triggers_abort_update_and_delete(full_ledger, raw, table):
    before = raw.execute(f"SELECT * FROM {table}").fetchall()
    assert before

    with pytest.raises(sqlite3.IntegrityError, match=f"{table} is append-only"):
        raw.execute(f"UPDATE {table} SET {TAMPERING[table]}")
    with pytest.raises(sqlite3.IntegrityError, match=f"{table} is append-only"):
        raw.execute(f"DELETE FROM {table}")

    raw.rollback()
    assert raw.execute(f"SELECT * FROM {table}").fetchall() == before


def test_replace_cannot_overwrite_a_row(full_ledger):
    # The Ledger's own connection turns on recursive_triggers, so the REPLACE conflict
    # resolution fires the DELETE triggers instead of silently swapping the row.
    db = full_ledger._db
    with pytest.raises(sqlite3.IntegrityError, match="holdout_spends is append-only"):
        db.execute(
            "INSERT OR REPLACE INTO holdout_spends (id, family_id, request_id, verdict,"
            " result_json) VALUES (1, 1, 1, 'PASS', '{}')"
        )
    with pytest.raises(sqlite3.IntegrityError, match="trial_combinations is append-only"):
        db.execute(
            "REPLACE INTO trial_combinations (trial_id, position, label, blind_sharpe)"
            " VALUES (1, 0, 'relabelled', 3.0)"
        )
    assert full_ledger.holdout_spend(1).verdict == "HOLDOUT FAIL"


# Spec §9.3: no update or delete statements in the source. The schema needs none either, so
# nothing is exempt; REPLACE and DROP are edits too.
EDIT = re.compile(
    r"\bUPDATE\s+\S+\s+SET\b|\bDELETE\s+FROM\b|\bOR\s+REPLACE\b|\bREPLACE\s+INTO\b"
    r"|\bDROP\s+(?:TABLE|TRIGGER)\b",
    re.IGNORECASE,
)


def find_edits(root: Path) -> list[tuple[str, int, str]]:
    """Every SQL edit statement in a string literal (f-strings included) under `root`."""
    hits = set()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.JoinedStr):
                text = "".join(
                    part.value if isinstance(part, ast.Constant) else "{}" for part in node.values
                )
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
            else:
                continue
            match = EDIT.search(text)
            if match:
                hits.add((path.name, node.lineno, match.group(0)))
    return sorted(hits)


def test_src_has_no_update_or_delete_statements():
    assert find_edits(SRC) == []


def test_scan_catches_edit_statements(tmp_path):
    (tmp_path / "plain.py").write_text("db.execute(\"UPDATE trials SET verdict = 'PASS'\")\n")
    (tmp_path / "fstring.py").write_text('db.execute(f"delete from {table} where id = 1")\n')
    (tmp_path / "upsert.py").write_text('db.execute("INSERT OR REPLACE INTO trials VALUES (1)")\n')
    (tmp_path / "prose.py").write_text(
        '"""Triggers abort UPDATE and DELETE on every table."""\nname = text.replace("a", "b")\n'
    )

    assert find_edits(tmp_path) == [
        ("fstring.py", 1, "delete from"),
        ("plain.py", 1, "UPDATE trials SET"),
        ("upsert.py", 1, "OR REPLACE"),
    ]


# Migrations


def test_migrations_run_once(path):
    with Ledger(path):
        pass
    with Ledger(path):  # reopening applies nothing; a rerun would fail on CREATE TABLE
        pass
    connection = sqlite3.connect(path)
    (version,) = connection.execute("PRAGMA user_version").fetchone()
    connection.close()
    assert version == len(MIGRATIONS)


def test_trials_from_before_rules_versions_survive_migration_2(path):
    # A Ledger made by migration 1 alone, holding one Trial, opened by today's code: the Trial
    # is kept as it was, with no rules version, and every new Trial must name one.
    old = sqlite3.connect(path)
    old.executescript(f"BEGIN;\n{MIGRATIONS[0]}\nPRAGMA user_version = 1;\nCOMMIT;")
    bars = BarsFingerprint.of(make_bars())
    old.execute("INSERT INTO families (idea) VALUES ('London breakout on gold')")
    old.execute(
        "INSERT INTO hypotheses (family_id, spec_json, entry_block, symbol, timeframe)"
        " VALUES (1, ?, 'donchian_breakout', 'GOLD', 'M15')",
        (validate(make_spec()).model_dump_json(),),
    )
    old.execute(
        "INSERT INTO trials (hypothesis_id, verdict, cost_json, bars_count, bars_first,"
        " bars_last, bars_sha256, result_json) VALUES (1, 'CANDIDATE', ?, ?, ?, ?, ?, '{}')",
        (COST.to_json(), bars.count, bars.first_time, bars.last_time, bars.sha256),
    )
    old.commit()
    old.close()

    with Ledger(path) as ledger:
        [legacy] = ledger.trials_for(1)
        new = ledger.trial(record(ledger, 1))
        raw = sqlite3.connect(path)
        with pytest.raises(sqlite3.IntegrityError, match="names the rules version"):
            raw.execute(
                "INSERT INTO trials (hypothesis_id, verdict, closest_miss_json, cost_json,"
                " bars_count, bars_first, bars_last, bars_sha256, result_json)"
                " SELECT hypothesis_id, verdict, closest_miss_json, cost_json, bars_count,"
                " bars_first, bars_last, bars_sha256, result_json FROM trials WHERE id = 2"
            )
        raw.close()

    assert (legacy.verdict, legacy.rules_version) == ("CANDIDATE", None)
    assert new.rules_version == RULES_VERSION


def test_a_newer_ledger_is_refused(path):
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    connection.close()

    with pytest.raises(LedgerError, match="schema version"):
        Ledger(path)


# Families and Hypotheses


def test_frozen_spec_reads_back_unchanged(path):
    spec = make_spec(mult=[1.5, 2.0, 2.5])
    with Ledger(path) as ledger:
        family = ledger.create_family("London breakout on gold")
        hypothesis = ledger.register_hypothesis(family, spec)

    with Ledger(path) as reopened:
        stored = reopened.hypothesis(hypothesis)

    assert stored.family_id == family
    assert stored.spec == validate(spec)
    assert validate(stored.spec_json) == validate(spec)
    assert json.loads(stored.spec_json) == validate(spec).model_dump(mode="json")
    assert (stored.entry_block, stored.symbol, stored.timeframe) == (
        "donchian_breakout",
        "GOLD",
        "M15",
    )


def test_a_validated_spec_object_freezes_the_same_json(ledger):
    family = ledger.create_family("London breakout on gold")
    from_dict = ledger.register_hypothesis(family, make_spec())
    from_model = ledger.register_hypothesis(family, validate(make_spec()))

    assert ledger.hypothesis(from_dict).spec_json == ledger.hypothesis(from_model).spec_json


def test_an_invalid_spec_is_not_registered(ledger, raw):
    family = ledger.create_family("London breakout on gold")

    with pytest.raises(SpecInvalid):
        ledger.register_hypothesis(family, make_spec(symbol="BTCUSD"))
    assert raw.execute("SELECT COUNT(*) FROM hypotheses").fetchone() == (0,)


def test_unknown_ids_and_blank_ideas_are_refused(ledger):
    with pytest.raises(ValueError, match="text"):
        ledger.create_family("   ")
    with pytest.raises(NotFound):
        ledger.register_hypothesis(99, make_spec())
    with pytest.raises(NotFound):
        ledger.trial_count(99)
    with pytest.raises(NotFound):
        ledger.hypothesis(99)
    with pytest.raises(NotFound):
        ledger.request_holdout(99)


def test_anti_reset_rule_refuses_the_same_entry_and_symbol_in_a_new_family(ledger, raw):
    first = ledger.create_family("London breakout on gold")
    ledger.register_hypothesis(first, make_spec())
    renamed = ledger.create_family("Gold momentum at the open")

    with pytest.raises(FamilyConflict) as refused:
        ledger.register_hypothesis(renamed, make_spec(mult=3.0, name="momentum"))

    assert refused.value.family_id == first
    assert f"Idea Family {first}" in str(refused.value)
    assert "London breakout on gold" in str(refused.value)
    assert raw.execute("SELECT COUNT(*) FROM hypotheses").fetchone() == (1,)


def test_anti_reset_rule_allows_the_owning_family_and_other_pairs(ledger):
    first = ledger.create_family("London breakout on gold")
    ledger.register_hypothesis(first, make_spec())
    ledger.register_hypothesis(first, make_spec(mult=3.0))  # same family: fine
    other = ledger.create_family("Something else")

    ledger.register_hypothesis(other, make_spec(symbol="EURUSD"))  # another symbol
    ledger.register_hypothesis(other, make_spec(entry=MA_CROSS))  # another entry Block


def test_anti_reset_rule_holds_in_the_database_too(ledger, raw):
    first = ledger.create_family("London breakout on gold")
    ledger.register_hypothesis(first, make_spec())
    renamed = ledger.create_family("Gold momentum at the open")

    with pytest.raises(sqlite3.IntegrityError, match="another Idea Family"):
        raw.execute(
            "INSERT INTO hypotheses (family_id, spec_json, entry_block, symbol, timeframe)"
            " VALUES (?, '{}', 'donchian_breakout', 'GOLD', 'M15')",
            (renamed,),
        )
    raw.rollback()


# Trials


def test_a_trial_reads_back_complete(ledger):
    family = ledger.create_family("London breakout on gold")
    hypothesis = ledger.register_hypothesis(family, make_spec(mult=[1.5, 2.0, 2.5]))
    grid = expand_grid(validate(ledger.hypothesis(hypothesis).spec_json))

    trial = record(ledger, hypothesis, sharpes=[0.1, -0.2, 0.3])
    stored = ledger.trial(trial)

    assert stored.hypothesis_id == hypothesis
    assert (stored.verdict, stored.closest_miss) == ("REJECT", MISS)
    assert stored.combinations == tuple(
        zip([c.label() for c in grid], [0.1, -0.2, 0.3], strict=True)
    )
    assert stored.cost == COST
    assert stored.bars == BarsFingerprint.of(make_bars())
    assert json.loads(stored.result_json) == {"folds": [], "checks": []}

    candidate = ledger.trial(record(ledger, hypothesis, verdict="CANDIDATE"))
    assert (candidate.verdict, candidate.closest_miss) == ("CANDIDATE", None)
    assert ledger.trials_for(hypothesis) == [stored, candidate]


def test_an_incomplete_or_inconsistent_trial_is_refused(ledger):
    family = ledger.create_family("London breakout on gold")
    hypothesis = ledger.register_hypothesis(family, make_spec(mult=[1.5, 2.0, 2.5]))
    grid = expand_grid(validate(ledger.hypothesis(hypothesis).spec_json))
    good = {
        "combinations": [(c, 0.1) for c in grid],
        "verdict": "REJECT",
        "closest_miss": MISS,
        "cost": COST,
        "bars": make_bars(),
        "rules_version": RULES_VERSION,
        "result_json": "{}",
    }
    bad = [
        ("rules version", {"rules_version": 0}),
        ("rules version", {"rules_version": None}),
        ("rules version", {"rules_version": True}),
        ("rules version", {"rules_version": 1.0}),
        ("whole grid", {"combinations": [(c, 0.1) for c in grid[:2]]}),
        ("whole grid", {"combinations": [(grid[0], 0.1), (grid[0], 0.2), (grid[1], 0.3)]}),
        ("finite", {"combinations": [(grid[0], float("nan"))] + [(c, 0.1) for c in grid[1:]]}),
        ("Closest Miss", {"closest_miss": None}),
        ("Closest Miss", {"verdict": "CANDIDATE"}),
        ("verdict", {"verdict": "PASS", "closest_miss": None}),
        ("EURUSD", {"bars": make_bars("EURUSD")}),
        ("Expecting value", {"result_json": "not json"}),
    ]
    for reason, change in bad:
        with pytest.raises(ValueError, match=reason):
            ledger.record_trial(hypothesis, **(good | change))
    with pytest.raises(NotFound):
        ledger.record_trial(99, **good)

    assert ledger.trial_count(family) == 0
    ledger.record_trial(hypothesis, **good)
    assert ledger.trial_count(family) == 1


def test_bars_fingerprint_changes_with_the_data():
    assert BarsFingerprint.of(make_bars()) == BarsFingerprint.of(make_bars())
    assert (
        BarsFingerprint.of(make_bars(shift=0.01)).sha256 != BarsFingerprint.of(make_bars()).sha256
    )
    fingerprint = BarsFingerprint.of(make_bars(n=8))
    assert (fingerprint.count, fingerprint.first_time, fingerprint.last_time) == (
        8,
        1_420_070_400,
        1_420_070_400 + 7 * 900,
    )


def test_budget_refuses_the_26th_trial_until_the_owner_grants_more(ledger, raw):
    family = ledger.create_family("London breakout on gold")
    gold = ledger.register_hypothesis(family, make_spec())
    eurusd = ledger.register_hypothesis(family, make_spec(symbol="EURUSD"))
    for i in range(TRIAL_BUDGET):  # the budget counts every Hypothesis in the family
        record(ledger, gold if i % 2 else eurusd)
    assert (ledger.trial_count(family), ledger.budget(family)) == (25, 25)

    with pytest.raises(BudgetExhausted, match="25 of its 25"):
        record(ledger, gold)
    assert ledger.trial_count(family) == 25

    assert ledger.extend_budget(family) == 50
    assert raw.execute("SELECT family_id, amount FROM budget_grants").fetchall() == [(family, 25)]
    record(ledger, gold)
    assert (ledger.trial_count(family), ledger.budget(family)) == (26, 50)

    ledger.extend_budget(family, 10)
    assert ledger.budget(family) == 60
    assert raw.execute("SELECT COUNT(*) FROM budget_grants").fetchone() == (2,)


def test_budget_belongs_to_one_family(ledger):
    spent = ledger.create_family("London breakout on gold")
    hypothesis = ledger.register_hypothesis(spent, make_spec())
    for _ in range(TRIAL_BUDGET):
        record(ledger, hypothesis)
    fresh = ledger.create_family("Something else")
    record(ledger, ledger.register_hypothesis(fresh, make_spec(entry=MA_CROSS)))

    assert (ledger.trial_count(fresh), ledger.budget(fresh)) == (1, 25)


@pytest.mark.parametrize("amount", [0, -25, 2.5, True])
def test_a_grant_is_a_positive_whole_number(ledger, amount):
    family = ledger.create_family("London breakout on gold")
    with pytest.raises(ValueError):
        ledger.extend_budget(family, amount)


def test_deflation_inputs_sum_every_combination_of_every_family_trial(ledger):
    family = ledger.create_family("London breakout on gold")
    gold = ledger.register_hypothesis(family, make_spec(mult=[1.5, 2.0, 2.5]))
    eurusd = ledger.register_hypothesis(family, make_spec(symbol="EURUSD"))
    other = ledger.create_family("Something else")
    record(ledger, ledger.register_hypothesis(other, make_spec(entry=MA_CROSS)), sharpes=[5.0])
    assert ledger.deflation_inputs(family) == (0, 0.0)

    record(ledger, gold, sharpes=[0.1, 0.2, 0.3])
    assert ledger.deflation_inputs(family) == (
        3,
        pytest.approx(statistics.variance([0.1, 0.2, 0.3])),
    )

    record(ledger, gold, sharpes=[0.1, 0.2, 0.3])  # the same grid again counts again
    record(ledger, eurusd, sharpes=[-0.4])
    sharpes = [0.1, 0.2, 0.3, 0.1, 0.2, 0.3, -0.4]
    assert ledger.deflation_inputs(family) == (7, pytest.approx(np.var(sharpes, ddof=1)))

    pending = [0.7, 0.8]
    assert ledger.deflation_inputs(family, pending) == (
        9,
        pytest.approx(np.var(sharpes + pending, ddof=1)),
    )
    assert ledger.deflation_inputs(other) == (1, 0.0)


# The Holdout


def test_a_grant_verifies_for_its_own_family_and_symbol_until_spent(ledger):
    family = ledger.create_family("London breakout on gold")
    hypothesis = ledger.register_hypothesis(family, make_spec())
    other = ledger.create_family("Something else")
    other_grant = approved(ledger, ledger.register_hypothesis(other, make_spec(symbol="EURUSD")))
    grant = approved(ledger, hypothesis)

    assert grant.family_id == family
    assert ledger.verify(grant, "GOLD")
    assert ledger.verify(grant, "GOLD")  # verifying spends nothing; M15 and M1 both ask
    assert not ledger.verify(grant, "EURUSD")
    assert not ledger.verify(HoldoutGrant(grant.request_id, other, grant.token), "GOLD")
    assert not ledger.verify(HoldoutGrant(grant.request_id, family, "forged"), "GOLD")
    assert not ledger.verify(HoldoutGrant(other_grant.request_id, family, grant.token), "GOLD")
    assert not ledger.verify(HoldoutGrant(999, family, grant.token), "GOLD")
    assert not ledger.verify(HoldoutGrant(2**70, family, grant.token), "GOLD")
    assert not ledger.verify((grant.request_id, family, grant.token), "GOLD")

    ledger.record_holdout(grant, "HOLDOUT FAIL", '{"trades": 41}')

    assert not ledger.verify(grant, "GOLD")
    assert ledger.verify(other_grant, "EURUSD")  # the other family still holds its Holdout
    spend = ledger.holdout_spend(family)
    assert (spend.request_id, spend.verdict, spend.result_json) == (
        grant.request_id,
        "HOLDOUT FAIL",
        '{"trades": 41}',
    )
    assert ledger.holdout_spend(other) is None


def test_a_second_spend_violates_unique(ledger, raw):
    family = ledger.create_family("London breakout on gold")
    hypothesis = ledger.register_hypothesis(family, make_spec())
    first, second = approved(ledger, hypothesis), approved(ledger, hypothesis)
    pending = ledger.request_holdout(hypothesis)
    ledger.record_holdout(first, "PASS", "{}")

    for grant in (first, second):
        with pytest.raises(HoldoutSpent) as refused:
            ledger.record_holdout(grant, "PASS", "{}")
        assert isinstance(refused.value.__cause__, sqlite3.IntegrityError)
        assert "UNIQUE constraint failed: holdout_spends" in str(refused.value.__cause__)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE.*holdout_spends.family_id"):
        raw.execute(
            "INSERT INTO holdout_spends (family_id, request_id, verdict, result_json)"
            " VALUES (?, ?, 'PASS', '{}')",
            (family, second.request_id),
        )
    raw.rollback()

    with pytest.raises(HoldoutSpent):
        ledger.request_holdout(hypothesis)
    with pytest.raises(HoldoutSpent):
        ledger.approve_holdout(pending)
    assert raw.execute("SELECT COUNT(*) FROM holdout_spends").fetchone() == (1,)


def test_only_a_grant_the_ledger_issued_can_be_spent(ledger, raw):
    family = ledger.create_family("London breakout on gold")
    grant = approved(ledger, ledger.register_hypothesis(family, make_spec()))

    for forged in [
        HoldoutGrant(grant.request_id, family, "forged"),
        HoldoutGrant(grant.request_id, family + 1, grant.token),
        HoldoutGrant(999, family, grant.token),
    ]:
        with pytest.raises(LedgerError, match="not issued"):
            ledger.record_holdout(forged, "PASS", "{}")
    with pytest.raises(ValueError, match="Holdout verdict"):
        ledger.record_holdout(grant, "CANDIDATE", "{}")

    assert raw.execute("SELECT COUNT(*) FROM holdout_spends").fetchone() == (0,)
    assert ledger.verify(grant, "GOLD")


def test_a_request_is_approved_once(ledger):
    family = ledger.create_family("London breakout on gold")
    request = ledger.request_holdout(ledger.register_hypothesis(family, make_spec()))
    first = ledger.approve_holdout(request)

    with pytest.raises(LedgerError, match="already approved"):
        ledger.approve_holdout(request)
    with pytest.raises(NotFound):
        ledger.approve_holdout(999)
    assert len(first.token) >= 40  # token_urlsafe(32)


def test_a_holdout_request_names_its_hypothesis(path):
    with Ledger(path) as ledger:
        family = ledger.create_family("an idea")
        hypothesis = ledger.register_hypothesis(family, make_spec())
        request = ledger.request_holdout(hypothesis)

        found = ledger.holdout_request(request)

        assert isinstance(found, HoldoutRequest)
        assert (found.id, found.hypothesis_id) == (request, hypothesis)
        with pytest.raises(NotFound):
            ledger.holdout_request(request + 1)
