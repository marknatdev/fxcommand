"""Tests for the Research Desk tools (spec §7.2, CCC-44).

Covers all 7 tools exposed to the Research Desk LLM:
1. `list_blocks`: returns the Block Library.
2. `describe_data`: returns statistics on the Research Window only.
3. `draft_spec`: validates a Strategy Spec and returns parameter grid size (0 Trials counted).
4. `register_hypothesis`: freezes a Hypothesis in the Ledger, respecting existing families.
5. `run_trial`: executes the Gauntlet, charges 1 Trial against budget, raises BudgetExhausted.
6. `get_trial`: returns stored results and metrics for a completed Trial from the Ledger.
7. `request_holdout`: requests spending Holdout through the gate in spec §6.3.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from vibedesk.blocks.registry import LIBRARY
from vibedesk.desk.tools import DeskContext, build_desk_tools
from vibedesk.engine.costs import CostSnapshot
from vibedesk.gauntlet.trial import HoldoutRefused
from vibedesk.ledger import BudgetExhausted, Ledger, NotFound
from vibedesk.market.data import MarketData
from vibedesk.market.grant import RESEARCH_START
from vibedesk.market.synthetic import (
    PLANTED_EDGE,
    PLANTED_EDGE_RANGE,
    SyntheticBarSource,
    SyntheticConfig,
)
from vibedesk.notify import NoticeKind, RecordingNotifier

COST = CostSnapshot(point=0.01, spread_floor_points=10.0, slippage_points=1.0)

SPEC = {
    "name": "donchian-test",
    "symbol": "GOLD",
    "timeframe": "M15",
    "direction": "both",
    "entry": {"block": "donchian_breakout", "params": {"period": 20}},
    "exit": {
        "stop": {"block": "atr_stop", "params": {"mult": 1.5}},
        "target": {"block": "r_target", "params": {"r": 2.0}},
    },
    "sizing": {"block": "fixed_risk", "params": {}},
}

RANGE = (date(2020, 1, 1), PLANTED_EDGE_RANGE[1])


@pytest.fixture
def ledger():
    with Ledger(":memory:") as store:
        yield store


@pytest.fixture
def synthetic_market(ledger):
    source = SyntheticBarSource(1, *RANGE, config=SyntheticConfig())
    return MarketData(source, ledger)


@pytest.fixture
def planted_market(ledger):
    source = SyntheticBarSource(1, *RANGE, config=PLANTED_EDGE)
    return MarketData(source, ledger)


def test_list_blocks_returns_entire_block_library():
    ctx = DeskContext(ledger=None, market=None, family_id=1)
    tools = build_desk_tools(ctx)
    list_tool = tools.get("list_blocks")
    assert list_tool is not None

    res = list_tool.fn()
    blocks = res["blocks"]
    assert len(blocks) == len(LIBRARY.all())
    names = {b["name"] for b in blocks}
    assert "donchian_breakout" in names
    assert "atr_stop" in names
    assert "fixed_risk" in names
    valid_kinds = {"entry", "filter", "stop", "target", "trailing", "time", "opposite", "sizing"}
    for b in blocks:
        assert b["kind"] in valid_kinds


def test_describe_data_returns_research_window_statistics(ledger, synthetic_market):
    ctx = DeskContext(ledger=ledger, market=synthetic_market, family_id=1, cost=COST)
    tools = build_desk_tools(ctx)
    desc_tool = tools.get("describe_data")
    assert desc_tool is not None

    res = desc_tool.fn("GOLD", "M15")
    assert res["symbol"] == "GOLD"
    assert res["timeframe"] == "M15"
    assert res["bar_count"] > 0
    assert res["start_date"] is not None
    assert res["end_date"] is not None
    assert res["spread_floor"] == 10.0
    assert res["atr_p50"] is not None
    assert res["atr_p50"] > 0

    # Ensure start date is on or after RESEARCH_START
    start_dt = datetime.fromisoformat(res["start_date"]).replace(tzinfo=UTC)
    assert start_dt >= RESEARCH_START

    # Rejects M1 timeframe
    with pytest.raises(ValueError, match="trading timeframes"):
        desc_tool.fn("GOLD", "M1")

    # Rejects unknown symbol
    with pytest.raises(ValueError, match="unknown symbol"):
        desc_tool.fn("UNKNOWN_SYM", "M15")

    # When market is unconfigured, describe_data is not registered
    unconfigured_ctx = DeskContext(ledger=ledger, market=None, family_id=1)
    assert build_desk_tools(unconfigured_ctx).get("describe_data") is None


def test_draft_spec_validates_and_normalizes():
    ctx = DeskContext(ledger=None, market=None, family_id=1)
    tools = build_desk_tools(ctx)
    draft_tool = tools.get("draft_spec")
    assert draft_tool is not None

    # Valid spec with single combination
    res = draft_tool.fn(SPEC)
    assert res["valid"] is True
    assert res["combinations"] == 1
    assert res["errors"] == []

    # Valid spec with parameter grid
    grid_spec = {
        **SPEC,
        "entry": {"block": "donchian_breakout", "params": {"period": [10, 20]}},
        "exit": {
            "stop": {"block": "atr_stop", "params": {"mult": [1.5, 2.0]}},
            "target": {"block": "r_target", "params": {"r": 2.0}},
        },
    }
    res_grid = draft_tool.fn(grid_spec)
    assert res_grid["valid"] is True
    assert res_grid["combinations"] == 4

    # Invalid spec missing stop
    bad_spec = {"name": "no-stop", "symbol": "GOLD", "timeframe": "M15", "direction": "both"}
    res_bad = draft_tool.fn(bad_spec)
    assert res_bad["valid"] is False
    assert res_bad["combinations"] == 0
    assert len(res_bad["errors"]) > 0


def test_register_hypothesis_freezes_and_enforces_anti_gaming(ledger, synthetic_market):
    family_1 = ledger.create_family("Trend following on GOLD")
    ctx = DeskContext(ledger=ledger, market=synthetic_market, family_id=family_1)
    tools = build_desk_tools(ctx)
    reg_tool = tools.get("register_hypothesis")
    assert reg_tool is not None

    # First registration links to family_1
    res1 = reg_tool.fn(SPEC)
    assert res1["hypothesis_id"] == 1
    assert res1["family_id"] == family_1
    assert res1["entry_block"] == "donchian_breakout"
    assert res1["symbol"] == "GOLD"

    # Anti-gaming rule (ADR 0003, spec §6.4):
    # A new chat with a different family trying to register the same entry_block & symbol
    # must attach to family_1 so Trial counts cannot reset
    family_2 = ledger.create_family("Another Idea on GOLD")
    ctx_family_2 = DeskContext(ledger=ledger, market=synthetic_market, family_id=family_2)
    tools_2 = build_desk_tools(ctx_family_2)

    res2 = tools_2.get("register_hypothesis").fn(SPEC)
    assert res2["family_id"] == family_1  # Reused family_1!
    assert res2["hypothesis_id"] == 2


def test_register_hypothesis_auto_creates_family_when_none_provided(ledger, synthetic_market):
    ctx = DeskContext(ledger=ledger, market=synthetic_market, family_id=0)
    tools = build_desk_tools(ctx)
    res = tools.get("register_hypothesis").fn(SPEC)
    assert res["hypothesis_id"] == 1
    assert res["family_id"] > 0
    assert ledger.family(res["family_id"]).idea == SPEC["name"]


def test_run_trial_charges_budget_and_refuses_when_exhausted(ledger, synthetic_market):
    notifier = RecordingNotifier()
    family_id = ledger.create_family("Budget test on GOLD")
    ctx = DeskContext(
        ledger=ledger,
        market=synthetic_market,
        family_id=family_id,
        cost=COST,
        notifier=notifier,
        use_m1=False,
    )
    tools = build_desk_tools(ctx)

    reg_res = tools.get("register_hypothesis").fn(SPEC)
    hyp_id = reg_res["hypothesis_id"]

    # Family default budget is TRIAL_BUDGET (25).
    # To test budget exhaustion quickly, run trials until budget limit.
    # We can set budget_grants or verify budget counting.
    assert ledger.trial_count(family_id) == 0
    assert ledger.budget(family_id) == 25

    # Run 1 Trial
    run_res = tools.get("run_trial").fn(hyp_id)
    assert run_res["trial_id"] == 1
    assert run_res["hypothesis_id"] == hyp_id
    assert run_res["verdict"] in ("CANDIDATE", "REJECT")
    assert ledger.trial_count(family_id) == 1

    # Simulate budget spent: if we artificially record trials up to budget
    # The Desk cannot extend budget. Next trial must raise BudgetExhausted.
    # Let's verify by exhausting the remaining 24 trials on this family.
    # Rather than running 24 slow trials, let's create a family and test
    # BudgetExhausted when trial_count >= budget:
    fam_small = ledger.create_family("Small budget family on EURUSD")
    eur_spec = {
        **SPEC,
        "symbol": "EURUSD",
        "entry": {"block": "ma_cross", "params": {"fast": 10, "slow": 20, "type": "sma"}},
    }
    hyp_small = ledger.register_hypothesis(fam_small, eur_spec)
    # Artificially exhaust budget by inserting records into trials
    # or testing the guard in run_trial directly:
    ctx_small = DeskContext(
        ledger=ledger,
        market=synthetic_market,
        family_id=fam_small,
        cost=CostSnapshot(point=0.00001, spread_floor_points=10.0, slippage_points=1.0),
        use_m1=False,
    )
    tools_small = build_desk_tools(ctx_small)
    # Run 1 trial
    tools_small.get("run_trial").fn(hyp_small)
    assert ledger.trial_count(fam_small) == 1

    # Manually check that when used >= budget, BudgetExhausted is raised
    # by exhausting budget:
    db = ledger._db
    for i in range(2, 26):
        db.execute(
            "INSERT INTO trials (id, hypothesis_id, verdict, closest_miss_json, cost_json,"
            " bars_count, bars_first, bars_last, bars_sha256, rules_version, result_json)"
            " VALUES (?, ?, 'REJECT', '{}', '{}', 100, 1000, 2000, 'hash', 2, '{}')",
            (100 + i, hyp_small),
        )
    assert ledger.trial_count(fam_small) == 25
    assert ledger.budget(fam_small) == 25

    with pytest.raises(BudgetExhausted):
        tools_small.get("run_trial").fn(hyp_small)


def test_get_trial_retrieves_stored_record(ledger, synthetic_market):
    ctx = DeskContext(
        ledger=ledger,
        market=synthetic_market,
        family_id=ledger.create_family("Get trial test"),
        cost=COST,
        use_m1=False,
    )
    tools = build_desk_tools(ctx)

    hyp_id = tools.get("register_hypothesis").fn(SPEC)["hypothesis_id"]
    trial_res = tools.get("run_trial").fn(hyp_id)
    trial_id = trial_res["trial_id"]

    fetched = tools.get("get_trial").fn(trial_id)
    assert fetched["trial_id"] == trial_id
    assert fetched["hypothesis_id"] == hyp_id
    assert fetched["verdict"] == trial_res["verdict"]
    assert "summary" in fetched
    assert "trades" in fetched["summary"]
    assert "trade_list" not in json.dumps(fetched)

    # Nonexistent trial raises NotFound
    with pytest.raises(NotFound):
        tools.get("get_trial").fn(99999)


def test_request_holdout_gate_refuses_reject_trial(ledger, synthetic_market):
    notifier = RecordingNotifier()
    ctx = DeskContext(
        ledger=ledger,
        market=synthetic_market,
        family_id=ledger.create_family("Holdout refusal test"),
        cost=COST,
        notifier=notifier,
        use_m1=False,
    )
    tools = build_desk_tools(ctx)

    hyp_id = tools.get("register_hypothesis").fn(SPEC)["hypothesis_id"]
    trial_res = tools.get("run_trial").fn(hyp_id)
    assert trial_res["verdict"] == "REJECT"

    # Requesting Holdout on a REJECT trial must be refused by the §6.3 gate
    with pytest.raises(HoldoutRefused, match="NOT_CANDIDATE"):
        tools.get("request_holdout").fn(hyp_id)

    # Notifier has not received HOLDOUT_REQUEST
    assert len(notifier.by_kind(NoticeKind.HOLDOUT_REQUEST)) == 0


def test_unconfigured_context_only_registers_context_free_tools():
    # Without ctx
    tools_no_ctx = build_desk_tools()
    assert {t.name for t in tools_no_ctx.all()} == {"list_blocks", "draft_spec"}

    # With ctx but None ledger/market
    ctx = DeskContext(ledger=None, market=None, family_id=1)
    tools = build_desk_tools(ctx)
    assert {t.name for t in tools.all()} == {"list_blocks", "draft_spec"}
    assert tools.get("describe_data") is None
    assert tools.get("register_hypothesis") is None
    assert tools.get("run_trial") is None
    assert tools.get("get_trial") is None
    assert tools.get("request_holdout") is None


def test_run_trial_refuses_when_no_cost_measured_and_does_not_charge_budget(
    ledger, synthetic_market, tmp_path
):
    family_id = ledger.create_family("Unmeasured cost refusal test")
    ctx = DeskContext(
        ledger=ledger,
        market=synthetic_market,
        family_id=family_id,
        cost=None,
        root=tmp_path,  # empty dir: no data/costs/*.jsonl exists
        use_m1=False,
    )
    tools = build_desk_tools(ctx)
    hyp_id = tools.get("register_hypothesis").fn(SPEC)["hypothesis_id"]

    # Refuses instead of falling back to 0 spread floor (honest verdict rule)
    with pytest.raises(
        ValueError, match="no Spread Floor measured for GOLD; run Update data first"
    ):
        tools.get("run_trial").fn(hyp_id)

    # Budget is NOT charged and no trial recorded in Ledger
    assert ledger.trial_count(family_id) == 0


def test_trial_tools_bound_content_size_for_llm_context(ledger, synthetic_market):
    ctx = DeskContext(
        ledger=ledger,
        market=synthetic_market,
        family_id=ledger.create_family("Bounded tool content size test"),
        cost=COST,
        use_m1=False,
    )
    tools = build_desk_tools(ctx)
    hyp_id = tools.get("register_hypothesis").fn(SPEC)["hypothesis_id"]

    run_res = tools.get("run_trial").fn(hyp_id)
    run_json = json.dumps(run_res)
    assert len(run_json) < 4096, f"run_trial output too large: {len(run_json)} bytes"
    assert "trade_list" not in run_json
    assert "daily_r" not in run_json
    assert "summary" in run_res
    run_checks = run_res["summary"]["checks"]
    assert len(run_checks) > 0
    for c in run_checks:
        assert c["name"] is not None and len(c["name"]) > 0
        assert c["status"] in ("pass", "fail", "n/a")

    get_res = tools.get("get_trial").fn(run_res["trial_id"])
    get_json = json.dumps(get_res)
    assert len(get_json) < 4096, f"get_trial output too large: {len(get_json)} bytes"
    assert "trade_list" not in get_json
    assert "daily_r" not in get_json
    assert "summary" in get_res
    get_checks = get_res["summary"]["checks"]
    assert len(get_checks) > 0
    for c in get_checks:
        assert c["name"] is not None and len(c["name"]) > 0
        assert c["status"] in ("pass", "fail", "n/a")
