"""The Research Desk loop (spec §7.1, §3.3): tool calls, SSE events (`message`, `tool_started`,
`trial_progress`, `verdict`), the 12-call cap, and tool error recovery, driven by `ScriptedLLM`."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import vibedesk.app.llm as llm_mod
from vibedesk.app.llm import OmniRoute, OmniRouteLLM, ScriptedLLM, ToolCall, Turn
from vibedesk.app.research import EXAMPLE
from vibedesk.app.server import create_app
from vibedesk.desk import (
    EVENT_TYPES,
    MAX_TOOL_CALLS,
    DeskLoop,
    Event,
    ToolRegistry,
    desk_loop,
    parse_sse,
    sse_stream,
    starter_tools,
)


def make_tools() -> tuple[ToolRegistry, list[tuple[str, dict]]]:
    """Minimal tool registry for testing the Research Desk loop."""
    calls: list[tuple[str, dict]] = []
    registry = starter_tools()

    @registry.register("register_hypothesis")
    def register_hypothesis(family_id: int, spec: dict) -> dict:
        """Freeze a Hypothesis in the Ledger."""
        calls.append(("register_hypothesis", {"family_id": family_id, "spec": spec}))
        return {"hypothesis_id": 7, "family_id": family_id}

    @registry.register("run_trial")
    def run_trial(hypothesis_id: int) -> dict:
        """Run the Gauntlet on a Hypothesis."""
        calls.append(("run_trial", {"hypothesis_id": hypothesis_id}))
        return {
            "trial_id": 1,
            "hypothesis_id": hypothesis_id,
            "verdict": "REJECT",
            "closest_miss": {"check": "oos_profit_factor", "value": 1.08, "threshold": 1.2},
        }

    return registry, calls


def test_normal_run_calls_tools_feeds_results_back_and_finishes_with_prose():
    registry, recorded = make_tools()
    llm = ScriptedLLM(
        ToolCall("list_blocks"),
        ToolCall("draft_spec", {"spec": EXAMPLE}),
        ToolCall("register_hypothesis", {"family_id": 1, "spec": EXAMPLE}),
        ToolCall("run_trial", {"hypothesis_id": 7}),
        "The Hypothesis was tested on the Research Window and rejected on profit factor.",
    )

    events = list(desk_loop(llm, "Test a GOLD H1 Donchian breakout", tools=registry))

    assert [e.event for e in events] == [
        "tool_started",
        "tool_started",
        "tool_started",
        "tool_started",
        "trial_progress",
        "verdict",
        "message",
    ]
    assert recorded == [
        ("register_hypothesis", {"family_id": 1, "spec": EXAMPLE}),
        ("run_trial", {"hypothesis_id": 7}),
    ]
    # Every tool schema was sent to the LLM on every call.
    schema_names = [s["function"]["name"] for s in llm.tools_seen[0] or []]
    assert schema_names == ["list_blocks", "draft_spec", "register_hypothesis", "run_trial"]

    # Each tool result was fed back into the conversation before the next LLM call.
    final_call = llm.calls[-1]
    tool_messages = [m for m in final_call if m["role"] == "tool"]
    assert [m["name"] for m in tool_messages] == [
        "list_blocks",
        "draft_spec",
        "register_hypothesis",
        "run_trial",
    ]
    assert json.loads(tool_messages[1]["content"])["combinations"] == 8
    assert json.loads(tool_messages[3]["content"])["verdict"] == "REJECT"

    final = events[-1]
    assert final.data["content"].startswith("The Hypothesis was tested")
    assert final.data["capped"] is False
    assert final.data["tool_calls"] == 4


def test_sse_event_order_and_wire_format_with_explicit_trial_progress():
    registry = ToolRegistry()

    @registry.register("describe_data")
    def describe_data(symbol: str, tf: str = "H1") -> dict:
        return {"symbol": symbol, "tf": tf, "spread_floor": 55}

    @registry.register("run_trial")
    def run_trial(hypothesis_id: int, emit) -> dict:
        emit("trial_progress", {"hypothesis_id": hypothesis_id, "stage": "walk_forward"})
        emit("trial_progress", {"hypothesis_id": hypothesis_id, "stage": "coin_flip_baseline"})
        return {"trial_id": 3, "hypothesis_id": hypothesis_id, "verdict": "CANDIDATE"}

    llm = ScriptedLLM(
        ToolCall("describe_data", {"symbol": "GOLD", "tf": "H1"}),
        ToolCall("run_trial", {"hypothesis_id": 2}),
        "The Hypothesis survived the Gauntlet as a CANDIDATE.",
    )
    loop = DeskLoop(llm, tools=registry)

    frames = list(loop.sse("Check GOLD H1 breakout"))
    parsed = parse_sse(frames)

    assert all(f.startswith("event: ") and "\ndata: " in f and f.endswith("\n\n") for f in frames)
    assert [e.event for e in parsed] == [
        "tool_started",
        "tool_started",
        "trial_progress",
        "trial_progress",
        "verdict",
        "message",
    ]
    assert [e.data["stage"] for e in parsed if e.event == "trial_progress"] == [
        "walk_forward",
        "coin_flip_baseline",
    ]
    assert parsed[4].data == {"trial_id": 3, "hypothesis_id": 2, "verdict": "CANDIDATE"}
    assert parsed[5].data["text"] == "The Hypothesis survived the Gauntlet as a CANDIDATE."


def test_the_twelve_tool_call_cap_stops_and_tells_the_owner_plainly():
    assert MAX_TOOL_CALLS == 12
    executed: list[int] = []
    registry = ToolRegistry()

    @registry.register("step")
    def step(n: int) -> dict:
        executed.append(n)
        return {"n": n}

    llm = ScriptedLLM(*[ToolCall("step", {"n": i}) for i in range(1, 16)])

    events = list(desk_loop(llm, "Keep calling tools forever", tools=registry))

    assert executed == list(range(1, 13))
    assert len(llm.calls) == 12
    started = [e for e in events if e.event == "tool_started"]
    assert len(started) == 12
    assert [e.data["step"] for e in started] == list(range(1, 13))

    final = events[-1]
    assert final.event == "message"
    assert final.data["capped"] is True
    assert final.data["tool_calls"] == 12
    assert "12 tool calls" in final.data["content"]
    assert "limit" in final.data["content"].lower()


def test_the_cap_slices_a_multi_call_turn_and_resets_on_the_next_owner_message():
    executed: list[int] = []
    registry = ToolRegistry({"step": lambda n: executed.append(n) or {"n": n}})
    llm = ScriptedLLM(
        [ToolCall("step", {"n": i}) for i in range(1, 11)],  # 10 calls in turn 1
        [ToolCall("step", {"n": i}) for i in range(11, 16)],  # asks for 5 more; only 2 may run
        ToolCall("step", {"n": 99}),
        "Second message finished cleanly.",
    )
    loop = DeskLoop(llm, tools=registry)

    first = loop.run("First message")
    second = loop.run("Second message")

    assert [e.event for e in first] == ["tool_started"] * 12 + ["message"]
    assert first[-1].data["capped"] is True
    assert executed == [*range(1, 13), 99]
    assert [e.event for e in second] == ["tool_started", "message"]
    assert second[-1].data["capped"] is False
    assert second[-1].data["content"] == "Second message finished cleanly."


def test_tool_errors_are_fed_back_to_the_llm_and_the_loop_recovers():
    registry = starter_tools()

    @registry.register("explode")
    def explode() -> dict:
        raise RuntimeError("temporary cache lock")

    bad_spec = {**EXAMPLE, "entry": {"block": "not_a_block", "params": {}}}
    llm = ScriptedLLM(
        ToolCall("draft_spec", {"spec": bad_spec}),
        ToolCall("unknown_tool", {"x": 1}),
        ToolCall("draft_spec", "{broken json"),
        ToolCall("explode"),
        ToolCall("draft_spec", {"spec": EXAMPLE}),
        "Recovered after fixing the Strategy Spec.",
    )

    events = list(desk_loop(llm, "Draft a breakout Strategy Spec", tools=registry))

    assert [e.event for e in events] == ["tool_started"] * 5 + ["message"]
    assert events[-1].data["content"] == "Recovered after fixing the Strategy Spec."

    tool_replies = [json.loads(m["content"]) for m in llm.calls[-1] if m["role"] == "tool"]
    assert "SpecInvalid" in tool_replies[0]["error"]
    assert tool_replies[1] == {"error": "unknown tool: unknown_tool"}
    assert "invalid arguments for draft_spec" in tool_replies[2]["error"]
    assert tool_replies[3] == {"error": "RuntimeError: temporary cache lock"}
    assert tool_replies[4]["valid"] is True and tool_replies[4]["combinations"] == 8


def test_omniroute_and_scripted_llm_share_the_same_tool_calling_seam(monkeypatch):
    assert OmniRouteLLM is OmniRoute
    sent_bodies: list[dict] = []
    http_replies = [
        httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_a1",
                                    "type": "function",
                                    "function": {"name": "list_blocks", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"total_tokens": 42},
            },
        ),
        httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "The Block Library is ready.",
                        }
                    }
                ],
                "usage": {"total_tokens": 55},
            },
        ),
    ]

    def fake_request(method, url, *, json=None, headers=None, timeout=None):
        sent_bodies.append(json)
        return http_replies.pop(0)

    monkeypatch.setattr(llm_mod.httpx, "request", fake_request)
    client = OmniRoute("http://127.0.0.1:20128/v1", "sk-test", "vibe-desk")

    events = list(desk_loop(client, "What Blocks exist?"))

    assert [e.event for e in events] == ["tool_started", "message"]
    assert events[0].data["tool"] == "list_blocks"
    assert events[1].data["content"] == "The Block Library is ready."
    assert "tools" in sent_bodies[0] and sent_bodies[0]["model"] == "vibe-desk"
    assert sent_bodies[1]["messages"][-1]["role"] == "tool"
    assert sent_bodies[1]["messages"][-1]["tool_call_id"] == "call_a1"


def test_event_rejects_unknown_event_types():
    assert set(EVENT_TYPES) == {"message", "tool_started", "trial_progress", "verdict"}
    with pytest.raises(ValueError, match="unknown event type"):
        Event("something_else", {})
    with pytest.raises(ValueError, match="at least 1"):
        list(desk_loop(ScriptedLLM("hi"), "idea", max_tool_calls=0))


def test_scripted_llm_supports_turns_dicts_and_errors():
    llm = ScriptedLLM(
        Turn(content="", tool_calls=(ToolCall("list_blocks"),), usage={"total_tokens": 10}),
        {"role": "assistant", "content": "Done.", "usage": {"total_tokens": 20}},
        RuntimeError("boom"),
    )

    first = llm.complete([{"role": "user", "content": "hi"}])
    second = llm.complete([{"role": "user", "content": "hi"}])

    assert first.tool_calls[0].id == "call_1" and first.usage == {"total_tokens": 10}
    assert second.content == "Done." and second.usage == {"total_tokens": 20}
    with pytest.raises(RuntimeError, match="boom"):
        llm.complete([])
    with pytest.raises(llm_mod.LlmError, match="ran out of scripted turns"):
        llm.complete([])


def test_fastapi_chat_endpoint_streams_sse_events(tmp_path):
    registry, _ = make_tools()
    llm = ScriptedLLM(
        ToolCall("run_trial", {"hypothesis_id": 1}),
        "The Trial finished with REJECT.",
    )
    client = TestClient(
        create_app(
            tmp_path / "data",
            llm=llm,
            settings_root=tmp_path / "settings",
            tools=registry,
        )
    )

    response = client.post("/api/chat", json={"message": "Run the Trial"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    assert [e.event for e in events] == [
        "tool_started",
        "trial_progress",
        "verdict",
        "message",
    ]
    assert events[2].data["verdict"] == "REJECT"
    assert events[3].data["content"] == "The Trial finished with REJECT."

    assert client.post("/api/chat", json={"message": "   "}).status_code == 400
    assert list(sse_stream(events)) == [e.to_sse() for e in events]


def test_llm_failure_streams_error_message_event_through_chat_endpoints(tmp_path):
    llm = ScriptedLLM(llm_mod.LlmError("OmniRoute said 503: resource pressure"))
    client = TestClient(
        create_app(
            tmp_path / "data",
            llm=llm,
            settings_root=tmp_path / "settings",
        )
    )

    # 1. /api/chat initial failure
    response = client.post("/api/chat", json={"message": "breakout strategy"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    assert len(events) == 1
    assert events[0].event == "message"
    assert events[0].data["role"] == "error"
    assert events[0].data["capped"] is False
    assert "OmniRoute said 503: resource pressure" in events[0].data["content"]
    assert events[0].data["text"] == events[0].data["content"]

    # 2. LLM failure after a tool call has already started
    registry, _ = make_tools()
    stepped_llm = ScriptedLLM(
        ToolCall("run_trial", {"hypothesis_id": 1}),
        llm_mod.LlmError("unexpected disconnect"),
    )
    stepped_client = TestClient(
        create_app(
            tmp_path / "data",
            llm=stepped_llm,
            settings_root=tmp_path / "settings",
            tools=registry,
        )
    )
    stepped_response = stepped_client.post("/api/chat", json={"message": "run trial"})
    assert stepped_response.status_code == 200
    stepped_events = parse_sse(stepped_response.text)
    assert [e.event for e in stepped_events] == [
        "tool_started",
        "trial_progress",
        "verdict",
        "message",
    ]
    assert stepped_events[-1].data["role"] == "error"
    assert "unexpected disconnect" in stepped_events[-1].data["content"]


def test_tool_returning_nan_inf_or_non_json_is_sanitized_without_crashing():
    from pathlib import Path

    registry = ToolRegistry()

    @registry.register("run_trial")
    def run_trial(hypothesis_id: int) -> dict:
        return {
            "hypothesis_id": hypothesis_id,
            "verdict": "CANDIDATE",
            "profit_factor": float("nan"),
            "unbounded": float("inf"),
            "neg_inf": float("-inf"),
            "path": Path("bars/GOLD"),
        }

    llm = ScriptedLLM(
        ToolCall("run_trial", {"hypothesis_id": 9}),
        "Trial completed despite special float values.",
    )
    loop = DeskLoop(llm, tools=registry)

    frames = list(loop.sse("Run trial with NaN metrics"))
    events = parse_sse(frames)

    assert [e.event for e in events] == [
        "tool_started",
        "trial_progress",
        "verdict",
        "message",
    ]
    verdict_event = events[2]
    assert verdict_event.data["profit_factor"] is None  # sanitized from NaN to null
    assert verdict_event.data["unbounded"] == "inf"
    assert verdict_event.data["neg_inf"] == "-inf"
    assert verdict_event.data["path"] == "bars/GOLD"

    # Verify tool result fed back to LLM was valid JSON with null and inf string
    last_call = llm.calls[-1]
    tool_msg = [m for m in last_call if m["role"] == "tool"][-1]
    tool_data = json.loads(tool_msg["content"])
    assert tool_data["profit_factor"] is None
    assert tool_data["unbounded"] == "inf"


def test_desk_loop_recovers_after_llm_error_without_poisoning_history():
    llm = ScriptedLLM(
        llm_mod.LlmError("503 Service Unavailable"),
        "I am ready now.",
    )
    loop = DeskLoop(llm)

    # First turn: LLM fails
    events1 = loop.run("First message that fails")
    assert len(events1) == 1
    assert events1[0].event == "message"
    assert events1[0].data["role"] == "error"
    assert "503" in events1[0].data["content"]

    # History was not poisoned with role 'error'
    assert not any(m.get("role") == "error" for m in loop.history)

    # Second turn: LLM succeeds
    events2 = loop.run("Second message that succeeds")
    assert len(events2) == 1
    assert events2[0].event == "message"
    assert events2[0].data["role"] == "assistant"
    assert events2[0].data["text"] == "I am ready now."

    # Assert no message with role 'error' reached the LLM in the second call
    last_call = llm.calls[-1]
    assert not any(m.get("role") == "error" for m in last_call)
    roles = [m["role"] for m in last_call]
    assert "error" not in roles


def test_starter_tools_exposes_only_context_free_tools_without_context():
    reg = starter_tools()
    schema_names = [s["function"]["name"] for s in reg.schemas()]
    assert schema_names == ["list_blocks", "draft_spec"]

    from vibedesk.desk.tools import DeskContext

    reg_unconfigured = starter_tools(DeskContext(ledger=None, market=None, family_id=0))
    assert [s["function"]["name"] for s in reg_unconfigured.schemas()] == [
        "list_blocks",
        "draft_spec",
    ]


# --- Grounding Rule wiring tests (spec §7.3, ADR 0005) ---


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


def test_grounding_rule_renders_placeholders_and_nested_spec_params():
    from datetime import date

    from vibedesk.desk.tools import DeskContext, build_desk_tools
    from vibedesk.engine.costs import CostSnapshot
    from vibedesk.ledger import Ledger
    from vibedesk.market.data import MarketData
    from vibedesk.market.synthetic import PLANTED_EDGE_RANGE, SyntheticBarSource, SyntheticConfig

    with Ledger(":memory:") as ledger:
        market = MarketData(
            SyntheticBarSource(
                1, date(2020, 1, 1), PLANTED_EDGE_RANGE[1], config=SyntheticConfig()
            ),
            ledger,
        )
        cost = CostSnapshot(point=0.01, spread_floor_points=10.0, slippage_points=1.0)
        family_id = ledger.create_family("Placeholder test")
        ctx = DeskContext(
            ledger=ledger,
            market=market,
            family_id=family_id,
            cost=cost,
            use_m1=False,
        )
        tools = build_desk_tools(ctx)

        hyp_id = tools.get("register_hypothesis").fn(EXAMPLE)["hypothesis_id"]
        trial_res = tools.get("run_trial").fn(hyp_id)
        trial_id = trial_res["trial_id"]
        verdict = trial_res["verdict"]

        # Model uses valid placeholders including nested spec parameter (period=20 from EXAMPLE)
        llm = ScriptedLLM(
            Turn(
                content=(
                    f"Evaluated {{{{hyp:{hyp_id}.symbol}}}} "
                    f"with period {{{{spec:{hyp_id}.period}}}} "
                    f"on {{{{date:2024-10-24}}}}. The verdict was {{{{trial:{trial_id}.verdict}}}} "
                    f"with profit factor {{{{trial:{trial_id}.pf}}}}."
                )
            )
        )

        events = list(desk_loop(llm, "Summary", tools=tools))
        assert len(events) == 1
        content = events[0].data["content"]
        assert "Evaluated GOLD with period [20, 55] on 2024-10-24." in content
        assert f"The verdict was {verdict}" in content


def test_grounding_rule_refuses_ambiguous_spec_param_and_retries():
    """If a spec parameter name appears in multiple blocks (e.g. entry and filter),

    lookup returns None, marking the placeholder unresolved so the model retries.
    """
    from vibedesk.desk.tools import DeskContext
    from vibedesk.ledger import Ledger

    with Ledger(":memory:") as ledger:
        spec_with_duplicate_param = {
            "name": "Donchian ATR ambiguous test",
            "symbol": "GOLD",
            "direction": "both",
            "timeframe": "H1",
            "entry": {"block": "donchian_breakout", "params": {"period": [20, 55]}},
            "filters": [
                {"block": "atr_regime", "params": {"period": 14, "lookback": 100}},
            ],
            "exit": {
                "stop": {"block": "atr_stop", "params": {"mult": 2.0}},
            },
            "sizing": {"block": "fixed_risk", "params": {"risk_pct": 1.0}},
        }
        family_id = ledger.create_family("Ambiguous spec test")
        hyp_id = ledger.register_hypothesis(family_id, spec_with_duplicate_param)
        ctx = DeskContext(ledger=ledger, market=None, family_id=family_id)
        from vibedesk.desk.tools import build_desk_tools

        tools = build_desk_tools(ctx)

        # Attempt 1 uses ambiguous {{spec:ID.period}} matching entry and filter (unresolved)
        # Model receives correction prompt citing unresolved placeholder and fixes on Attempt 2
        llm = ScriptedLLM(
            Turn(content=f"Strategy used period {{{{spec:{hyp_id}.period}}}} on GOLD."),
            Turn(content="The strategy evaluation completed without edge."),
        )

        events = list(desk_loop(llm, "Check results", tools=tools))
        assert len(events) == 1
        assert events[0].event == "message"
        assert events[0].data["content"] == "The strategy evaluation completed without edge."
        assert len(llm.calls) == 2

        # Check that correction message warned about unresolved placeholder
        second_call = llm.calls[1]
        user_msgs = [m for m in second_call if m["role"] == "user"]
        assert any("unresolved placeholder" in m["content"] for m in user_msgs)
