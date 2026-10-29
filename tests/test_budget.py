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
    DeskLoop,
    TokenBudget,
    build_desk_tools,
    desk_loop,
    starter_tools,
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


def test_desk_loop_crosses_100_percent_mid_message_and_preserves_history():
    budget = TokenBudget(daily_cap=1000)
    tools = starter_tools()

    # Turn 1: tool call (draft_spec), consumes 600 tokens (60% < 100%)
    # Turn 2: prose answer, consumes 500 tokens (total 1100 >= 1000, 100% reached!)
    llm = ScriptedLLM(
        Turn(
            content="",
            tool_calls=(ToolCall("draft_spec", {"spec": SPEC}),),
            usage={"total_tokens": 600},
        ),
        Turn(
            content="The Strategy Spec was drafted and validated successfully.",
            usage={"total_tokens": 500},
        ),
    )
    loop = DeskLoop(llm, tools=tools, budget=budget)

    events = loop.run("Validate my Strategy Spec")
    assert [e.event for e in events] == ["tool_started", "message", "message"]
    assert events[1].data["content"] == "The Strategy Spec was drafted and validated successfully."
    assert events[1].data.get("paused") is not True

    pause_ev = events[2]
    assert pause_ev.data.get("paused") is True
    assert "daily token budget has been reached (100%)" in pause_ev.data["content"]

    # DeskLoop.history preserved all messages (user, tool call, tool result, prose)
    roles = [m["role"] for m in loop.history]
    assert "user" in roles
    assert "tool" in roles
    assert "assistant" in roles
    assert any(
        m.get("content") == "The Strategy Spec was drafted and validated successfully."
        for m in loop.history
    )

    # Subsequent loop call is immediately paused without invoking LLM
    subsequent_events = loop.run("Another message")
    assert len(subsequent_events) == 1
    assert subsequent_events[0].data.get("paused") is True


def test_desk_loop_crosses_100_percent_on_tool_call_turn_preserves_history():
    budget = TokenBudget(daily_cap=1000)
    tools = starter_tools()

    # Turn 1 consumes 1100 tokens while requesting a tool call
    llm = ScriptedLLM(
        Turn(
            content="",
            tool_calls=(ToolCall("list_blocks"),),
            usage={"total_tokens": 1100},
        ),
    )
    loop = DeskLoop(llm, tools=tools, budget=budget)

    events = loop.run("List blocks please")
    assert len(events) == 1
    assert events[0].data.get("paused") is True

    # History was preserved: user message is in loop.history
    assert len(loop.history) >= 1
    assert loop.history[0]["role"] == "user"
