"""Tests for Token Budget (spec §7.4, CCC-46), Grounding Rule retries, and M4 end-to-end.

Covers:
1. TokenBudget usage tracking, 80% warning via Notifier, 100% pause of Desk loop.
2. Manual Trials permitted while Desk loop is paused.
3. Daily reset at 00:00 Asia/Bangkok time.
4. Grounding Rule retries in Desk loop (up to 2 retries, withholding explanation on failure).
5. Placeholder rendering from stored trial/hypothesis data.
6. M4 Done-When: scripted Idea reaches a Verdict end-to-end with ScriptedLLM (spec §10).
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from vibedesk.app.llm import ScriptedLLM, ToolCall, Turn
from vibedesk.desk import (
    BANGKOK_TZ,
    BudgetState,
    DeskContext,
    TokenBudget,
    build_desk_tools,
    desk_loop,
)
from vibedesk.engine.costs import CostSnapshot
from vibedesk.ledger import Ledger
from vibedesk.market.data import MarketData
from vibedesk.market.synthetic import PLANTED_EDGE_RANGE, SyntheticBarSource, SyntheticConfig
from vibedesk.notify import NoticeKind, RecordingNotifier

COST = CostSnapshot(point=0.01, spread_floor_points=10.0, slippage_points=1.0)
RANGE = (date(2020, 1, 1), PLANTED_EDGE_RANGE[1])

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


@pytest.fixture
def ledger():
    with Ledger(":memory:") as store:
        yield store


@pytest.fixture
def market(ledger):
    source = SyntheticBarSource(1, *RANGE, config=SyntheticConfig())
    return MarketData(source, ledger)


def test_token_budget_80_percent_warning_and_pause_thresholds():
    current_time = datetime(2026, 10, 28, 10, 0, tzinfo=BANGKOK_TZ)
    budget = TokenBudget(daily_cap=1_000, now=lambda: current_time)

    # 1. Below 80%: state is OK
    budget.record_usage(500)
    assert budget.state() == BudgetState.OK
    assert not budget.needs_warning
    assert not budget.is_paused

    # 2. Reach 80%: state is WARN, needs warning
    budget.record_usage(300)
    assert budget.state() == BudgetState.WARN
    assert budget.needs_warning
    assert budget.used == 800

    # Mark warned simulates notifier dispatch
    budget.mark_warned()
    assert not budget.needs_warning

    # 3. Reach 100%: state is PAUSED
    budget.record_usage(200)
    assert budget.state() == BudgetState.PAUSED
    assert budget.is_paused
    assert budget.remaining == 0


def test_token_budget_resets_at_bangkok_midnight():
    clock_time = datetime(2026, 10, 28, 23, 30, tzinfo=BANGKOK_TZ)
    budget = TokenBudget(daily_cap=1_000, now=lambda: clock_time)
    budget.record_usage(1_000)
    assert budget.is_paused

    # Clock moves to 00:05 Bangkok next day -> resets!
    clock_time = datetime(2026, 10, 29, 0, 5, tzinfo=BANGKOK_TZ)
    assert budget.state() == BudgetState.OK
    assert budget.used == 0
    assert not budget.is_paused
    assert not budget.needs_warning


def test_desk_loop_triggers_warning_notice_at_80_percent():
    notifier = RecordingNotifier()
    budget = TokenBudget(daily_cap=1_000)

    # LLM turn returns usage crossing 80% (850 tokens)
    llm = ScriptedLLM(Turn(content="The strategy was reviewed.", usage={"total_tokens": 850}))

    events = list(desk_loop(llm, "Review strategy", budget=budget, notifier=notifier))
    assert len(events) == 1
    assert events[0].event == "message"
    assert events[0].data["content"] == "The strategy was reviewed."

    # Notifier must have received TOKEN_WARNING notice
    warnings = notifier.by_kind(NoticeKind.TOKEN_WARNING)
    assert len(warnings) == 1
    assert "80%" in warnings[0].text
    assert warnings[0].data["used"] == 850


def test_desk_loop_pauses_at_100_percent_explaining_clearly_to_owner():
    notifier = RecordingNotifier()
    budget = TokenBudget(daily_cap=1_000)
    budget.record_usage(1_000)  # At 100% cap

    llm = ScriptedLLM("This should not be called")
    events = list(desk_loop(llm, "Check idea", budget=budget, notifier=notifier))

    assert len(events) == 1
    msg_event = events[0]
    assert msg_event.event == "message"
    assert msg_event.data["paused"] is True
    assert "paused" in msg_event.data["content"].lower()
    assert "manual trials are still permitted" in msg_event.data["content"].lower()
    assert len(llm.calls) == 0  # LLM was not called


def test_manual_trials_still_work_when_desk_loop_is_paused(ledger, market):
    # Token budget is paused at 100%
    budget = TokenBudget(daily_cap=1_000)
    budget.record_usage(1_000)
    assert budget.is_paused

    ctx = DeskContext(
        ledger=ledger,
        market=market,
        family_id=ledger.create_family("Manual trial test"),
        budget=budget,
        cost=COST,
        use_m1=False,
    )
    tools = build_desk_tools(ctx)

    # Manual trial execution via desk tools still works!
    hyp_res = tools.get("register_hypothesis").fn(SPEC)
    hyp_id = hyp_res["hypothesis_id"]

    trial_res = tools.get("run_trial").fn(hyp_id)
    assert trial_res["trial_id"] == 1
    assert trial_res["verdict"] in ("CANDIDATE", "REJECT")
    assert ledger.trial_count(hyp_res["family_id"]) == 1


def test_grounding_rule_retries_and_recovers_when_model_fixes_prose():
    # Attempt 1 contains raw digits ("profit factor was 1.55")
    # Model receives correction prompt and fixes on Attempt 2
    llm = ScriptedLLM(
        Turn(content="The profit factor was 1.55 on the blind years."),
        Turn(content="The strategy was rejected due to lack of edge."),
    )

    events = list(desk_loop(llm, "Check results"))
    assert len(events) == 1
    assert events[0].event == "message"
    assert events[0].data["content"] == "The strategy was rejected due to lack of edge."
    assert len(llm.calls) == 2

    # Check that correction message was sent to LLM
    second_call = llm.calls[1]
    user_msgs = [m for m in second_call if m["role"] == "user"]
    assert any("Grounding Rule violation" in m["content"] for m in user_msgs)


def test_grounding_rule_withholds_explanation_after_two_failed_retries():
    # Model fails 3 times in a row (initial + 2 retries)
    llm = ScriptedLLM(
        Turn(content="Profit factor was 1.5."),
        Turn(content="Win rate was 60%."),
        Turn(content="Net return was 250 points."),
    )

    events = list(desk_loop(llm, "Check results"))
    assert len(events) == 1
    assert events[0].event == "message"
    assert events[0].data["grounding_withheld"] is True
    assert "explanation was withheld" in events[0].data["content"].lower()
    assert len(llm.calls) == 3


def test_grounding_rule_renders_placeholders_from_stored_data(ledger, market):
    family_id = ledger.create_family("Placeholder test")
    ctx = DeskContext(
        ledger=ledger,
        market=market,
        family_id=family_id,
        cost=COST,
        use_m1=False,
    )
    tools = build_desk_tools(ctx)

    # Register and run trial 1
    hyp_id = tools.get("register_hypothesis").fn(SPEC)["hypothesis_id"]
    trial_res = tools.get("run_trial").fn(hyp_id)
    trial_id = trial_res["trial_id"]
    verdict = trial_res["verdict"]

    # Model uses valid placeholders
    llm = ScriptedLLM(
        Turn(
            content=f"Evaluated {{{{hyp:{hyp_id}.symbol}}}} on {{{{date:2024-10-24}}}}. "
            f"The verdict was {{{{trial:{trial_id}.verdict}}}}."
        )
    )

    events = list(desk_loop(llm, "Summary", tools=tools))
    assert len(events) == 1
    content = events[0].data["content"]
    assert f"Evaluated GOLD on 2024-10-24. The verdict was {verdict}." == content


def test_m4_scripted_idea_reaches_verdict_end_to_end(ledger, market):
    """M4 done-when (spec §10): a scripted Idea reaches a Verdict end to end with ScriptedLLM."""
    notifier = RecordingNotifier()
    budget = TokenBudget(daily_cap=100_000)
    ctx = DeskContext(
        ledger=ledger,
        market=market,
        family_id=0,
        budget=budget,
        notifier=notifier,
        cost=COST,
        use_m1=False,
    )
    tools = build_desk_tools(ctx)

    # Scripted LLM simulating end-to-end idea evaluation:
    # 1. Draft and validate spec
    # 2. Register frozen hypothesis
    # 3. Run trial through Gauntlet
    # 4. Return grounded summary with placeholders
    llm = ScriptedLLM(
        Turn(
            content="",
            tool_calls=(ToolCall("draft_spec", {"spec": SPEC}),),
            usage={"total_tokens": 120},
        ),
        Turn(
            content="",
            tool_calls=(ToolCall("register_hypothesis", {"spec": SPEC}),),
            usage={"total_tokens": 150},
        ),
        Turn(
            content="",
            tool_calls=(ToolCall("run_trial", {"hypothesis_id": 1}),),
            usage={"total_tokens": 200},
        ),
        Turn(
            content=(
                "Tested {{hyp:1.symbol}} on {{hyp:1.timeframe}}. Verdict is {{trial:1.verdict}}."
            ),
            usage={"total_tokens": 80},
        ),
    )

    events = list(desk_loop(llm, "Test a GOLD breakout strategy", tools=tools))

    # Verify event types and order
    event_names = [e.event for e in events]
    assert event_names == [
        "tool_started",
        "tool_started",
        "tool_started",
        "trial_progress",
        "verdict",
        "message",
    ]

    # Verify verdict event
    verdict_event = next(e for e in events if e.event == "verdict")
    assert verdict_event.data["trial_id"] == 1
    assert verdict_event.data["verdict"] in ("CANDIDATE", "REJECT")

    # Verify message event with placeholders properly rendered
    message_event = events[-1]
    verdict_str = verdict_event.data["verdict"]
    assert message_event.data["content"] == f"Tested GOLD on M15. Verdict is {verdict_str}."

    # Verify Ledger state
    assert ledger.trial_count(1) == 1
    tr = ledger.trial(1)
    assert tr.verdict == verdict_str

    # Verify token budget recorded usage (120 + 150 + 200 + 80 = 550)
    assert budget.used == 550
    assert budget.state() == BudgetState.OK
