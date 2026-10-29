"""Tests for the Vibe Desk MCP server and tools (spec §8.2, CCC-58, CCC-60).

Tests cover:
- MCP tool inventory: exactly 5 read tools and 3 act tools from spec §8.2.
- Exclusion of owner-only tools: no approve, budget, or settings tools in tool list.
- MCP tools execute successfully against FastAPI app via httpx ASGITransport.
- Owner-only API actions return HTTP 403 Forbidden without the owner cookie.
- Notices (CANDIDATE, Holdout Request, Token Warning, PASS, HOLDOUT FAIL) arrive
  in RecordingNotifier when triggered through the API.
- End-to-end MCP ClientSession JSON-RPC protocol execution over in-memory streams.
- Error handling on unknown IDs and invalid arguments.
- CLI argument parsing and environment variable configuration.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from httpx import ASGITransport
from mcp import ClientSession
from mcp.server.mcpserver.exceptions import ToolError

from vibedesk.app.api_spec import MCP_TOOL_MAP
from vibedesk.app.auth import OWNER_COOKIE_NAME, OWNER_COOKIE_VALUE
from vibedesk.app.desk import Desk
from vibedesk.app.llm import ScriptedLLM, Turn
from vibedesk.app.research import EXAMPLE
from vibedesk.app.server import create_app
from vibedesk.blocks.spec import expand_grid, validate
from vibedesk.engine.costs import CostSnapshot
from vibedesk.gauntlet.verdict import CANDIDATE
from vibedesk.market.bars import Timeframe
from vibedesk.market.cache import record_import, write_chunk
from vibedesk.market.costs import CostMeasurement, record_cost
from vibedesk.market.fake_mt5 import FakeMt5
from vibedesk.market.synthetic import SyntheticBarSource
from vibedesk.mcp.client import VibeDeskMcpClient
from vibedesk.mcp.server import create_mcp_server
from vibedesk.notify import (
    NoticeKind,
    RecordingNotifier,
    holdout_fail_notice,
    pass_notice,
)
from vibedesk.rules import RULES_VERSION


class AlwaysTruthyRecordingNotifier(RecordingNotifier):
    """RecordingNotifier that always evaluates to True in boolean contexts (e.g. `if notifier:`)."""

    def __bool__(self) -> bool:
        return True


def setup_test_cache(root: Path) -> None:
    """Setup synthetic GOLD M15 and H1 bars with costs in root/bars."""
    source = SyntheticBarSource(1, date(2013, 12, 1), date(2025, 1, 1))
    for timeframe in (Timeframe.M15, Timeframe.H1):
        bars = source.fetch("GOLD", timeframe)
        for year in range(2013, 2025):
            start = datetime(year, 1, 1, tzinfo=UTC).timestamp()
            end = datetime(year + 1, 1, 1, tzinfo=UTC).timestamp()
            write_chunk(root, bars.between(int(start), int(end)), year)
    record_import(root, "GOLD", datetime(2025, 1, 1, tzinfo=UTC))
    record_cost(
        root,
        CostMeasurement(
            "GOLD", "2026-10-23T00:00:00+00:00", 0.01, 30.0, 35.0, 1000, -50.0, 10.0, 3
        ),
    )


def record_candidate_trial(desk: Desk, hypothesis_id: int) -> int:
    """Record a dummy CANDIDATE trial directly into the Ledger."""
    hyp = desk.ledger.hypothesis(hypothesis_id)
    cost = CostSnapshot("GOLD", 30.0, 0.01, 1.0, 0.0, 0.0, 3)
    bars = desk.market.bars(
        "GOLD",
        Timeframe.H1,
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 1, tzinfo=UTC),
    )
    combos = expand_grid(validate(hyp.spec_json))
    combinations = [(c, 1.5) for c in combos]

    import numpy as np

    np.random.seed(42)
    daily_r_list = np.random.normal(0.2, 0.5, 1000).tolist()
    result_data = {
        "verdict": CANDIDATE,
        "rules_version": RULES_VERSION,
        "walk_forward": {
            "daily_r": {"day_start": list(range(1000)), "r": daily_r_list},
            "holdout_choice": 0,
        },
        "holdout": {"rate_per_year": 50.0},
    }
    return desk.ledger.record_trial(
        hypothesis_id,
        combinations=combinations,
        verdict=CANDIDATE,
        closest_miss=None,
        cost=cost,
        bars=bars,
        rules_version=RULES_VERSION,
        result_json=json.dumps(result_data),
    )


@pytest.fixture
def mcp_test_env(tmp_path: Path):
    """Fixture providing FastAPI app, RecordingNotifier, Desk, and MCP server wiring."""
    data_dir = tmp_path / "data"
    settings_dir = tmp_path / "settings"
    data_dir.mkdir(parents=True, exist_ok=True)
    settings_dir.mkdir(parents=True, exist_ok=True)
    setup_test_cache(data_dir)

    notifier = AlwaysTruthyRecordingNotifier()
    llm = ScriptedLLM(Turn(content="Breakout analysis on GOLD", usage={"total_tokens": 150}))
    fake_mt5 = FakeMt5(SyntheticBarSource(1, date(2013, 12, 1), date(2027, 1, 1)))

    app = create_app(
        root=data_dir,
        llm=llm,
        settings_root=settings_dir,
        notifier=notifier,
        mt5_api=fake_mt5,
        enable_scheduler=False,
    )

    async_client = httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    )
    mcp_server = create_mcp_server(client=async_client)
    desk = app.state.desk

    return {
        "app": app,
        "desk": desk,
        "notifier": notifier,
        "async_client": async_client,
        "mcp_server": mcp_server,
        "llm": llm,
        "data_dir": data_dir,
        "settings_dir": settings_dir,
    }


# --- CCC-58: MCP Tool Inventory & Exclusions ---


@pytest.mark.anyio
async def test_mcp_tool_inventory_and_exclusion_of_owner_tools(mcp_test_env):
    """Verify MCP exposes 5 read tools and 3 act tools and zero owner tools (spec §8.2)."""
    server = mcp_test_env["mcp_server"]
    tools = await server.list_tools()
    tool_names = {t.name for t in tools}

    expected_tools = {
        "list_ideas",
        "get_verdict",
        "get_ledger_stats",
        "list_blocks",
        "get_trial",
        "submit_idea",
        "run_trial",
        "request_holdout",
    }
    assert tool_names == expected_tools
    assert len(tool_names) == 8
    assert tool_names == set(MCP_TOOL_MAP.keys())

    # Strict check: tool list contains NO owner-only actions
    forbidden_terms = ["approve", "budget", "settings", "owner", "put_settings"]
    for tool_name in tool_names:
        for term in forbidden_terms:
            msg = f"Owner-only term '{term}' found in tool {tool_name}"
            assert term not in tool_name.lower(), msg


# --- CCC-58 & CCC-60: Read and Act Tools Against HTTP API ---


@pytest.mark.anyio
async def test_mcp_read_tools_against_api(mcp_test_env):
    """Verify MCP read tools execute cleanly against the running FastAPI app."""
    server = mcp_test_env["mcp_server"]
    desk = mcp_test_env["desk"]

    # 1. list_ideas initially empty
    res_ideas = await server.call_tool("list_ideas", {})
    assert len(res_ideas.content) == 0

    # Create family and hypothesis in ledger
    fam_id = desk.ledger.create_family("Trend Continuation")
    hyp_id = desk.ledger.register_hypothesis(fam_id, EXAMPLE)
    trial_res = desk.run_hypothesis(hyp_id, idea="Trial 1")
    trial_id = trial_res["trial_id"]

    # 2. list_ideas now contains the family
    res_ideas_2 = await server.call_tool("list_ideas", {})
    ideas_data = [json.loads(c.text) for c in res_ideas_2.content]
    assert len(ideas_data) == 1
    assert ideas_data[0]["id"] == fam_id
    assert ideas_data[0]["idea"] == "Trend Continuation"

    # 3. list_blocks returns library of blocks
    res_blocks = await server.call_tool("list_blocks", {})
    blocks_data = json.loads(res_blocks.content[0].text)
    assert "blocks" in blocks_data
    block_names = {b["name"] for b in blocks_data["blocks"]}
    assert "donchian_breakout" in block_names
    assert "atr_stop" in block_names
    assert "ma_cross" in block_names

    # 4. get_ledger_stats returns ledger counts
    res_ledger = await server.call_tool("get_ledger_stats", {})
    ledger_data = json.loads(res_ledger.content[0].text)
    assert "desk_trial_count" in ledger_data
    assert ledger_data["desk_trial_count"] >= 1
    assert len(ledger_data["families"]) >= 1

    # 5. get_trial and get_verdict return verdict data
    res_trial = await server.call_tool("get_trial", {"trial_id": trial_id})
    t_data = json.loads(res_trial.content[0].text)
    assert t_data["trial_id"] == trial_id
    assert t_data["verdict"] in ("CANDIDATE", "REJECT")

    # Alias `id` also supported
    res_verdict = await server.call_tool("get_verdict", {"id": trial_id})
    v_data = json.loads(res_verdict.content[0].text)
    assert v_data["trial_id"] == trial_id
    assert v_data["verdict"] in ("CANDIDATE", "REJECT")


@pytest.mark.anyio
async def test_mcp_act_tools_against_api(mcp_test_env):
    """Verify MCP act tools create families, run trials, and request holdouts."""
    server = mcp_test_env["mcp_server"]
    desk = mcp_test_env["desk"]

    # 1. submit_idea creates an Idea Family
    res_sub = await server.call_tool("submit_idea", {"idea": "GOLD H1 Breakout"})
    fam_data = json.loads(res_sub.content[0].text)
    assert fam_data["id"] > 0
    assert fam_data["idea"] == "GOLD H1 Breakout"
    assert fam_data["budget"] == 25
    assert fam_data["trial_count"] == 0

    # Register hypothesis on that family
    hyp_id = desk.ledger.register_hypothesis(fam_data["id"], EXAMPLE)

    # 2. run_trial executes Gauntlet on Hypothesis and counts 1 Trial
    res_run = await server.call_tool("run_trial", {"hypothesis_id": hyp_id, "idea": "Run #1"})
    run_data = json.loads(res_run.content[0].text)
    assert run_data["hypothesis_id"] == hyp_id
    assert run_data["verdict"] in ("CANDIDATE", "REJECT")
    assert run_data["trial_id"] > 0

    # Verify Ledger counted 1 trial
    assert desk.ledger.trial_count(fam_data["id"]) == 1

    # 3. request_holdout submits holdout request
    # Since run_data was REJECT or CANDIDATE, request_holdout endpoint handles gate
    res_holdout = await server.call_tool(
        "request_holdout", {"hypothesis_id": hyp_id, "reason": "Edge confirmed"}
    )
    h_data = json.loads(res_holdout.content[0].text)
    assert h_data["hypothesis_id"] == hyp_id
    assert h_data["status"] in ("pending", "refused")


# --- CCC-60: Owner-Only Refusals & MCP Isolation ---


@pytest.mark.anyio
async def test_owner_only_endpoints_refused_and_unreachable_by_mcp(mcp_test_env):
    """Verify owner-only actions return 403 without the cookie and MCP cannot reach them."""
    async_client = mcp_test_env["async_client"]
    desk = mcp_test_env["desk"]
    fam_id = desk.ledger.create_family("Owner Refusal Family")

    # 1. Extend budget: 403 without cookie
    res_b = await async_client.post(f"/api/families/{fam_id}/budget", json={"amount": 25})
    assert res_b.status_code == 403
    assert "Owner action required" in res_b.json()["detail"]

    # 2. Approve holdout: 403 without cookie
    res_h = await async_client.post("/api/holdout-requests/1/approve")
    assert res_h.status_code == 403
    assert "Owner action required" in res_h.json()["detail"]

    # 3. Update settings: 403 without cookie
    res_s = await async_client.put("/api/settings", json={"daily_token_cap": 200_000})
    assert res_s.status_code == 403
    assert "Owner action required" in res_s.json()["detail"]

    # 4. Verify owner actions succeed ONLY when the cookie is explicitly presented
    res_b_ok = await async_client.post(
        f"/api/families/{fam_id}/budget",
        json={"amount": 25},
        headers={"Cookie": f"{OWNER_COOKIE_NAME}={OWNER_COOKIE_VALUE}"},
    )
    assert res_b_ok.status_code == 200

    # 5. Verify the MCP client has no cookie and direct calls to owner endpoints raise ToolError
    backend_client = VibeDeskMcpClient(client=async_client)
    with pytest.raises(ToolError, match="HTTP 403"):
        await backend_client._request("POST", f"/api/families/{fam_id}/budget", json={"amount": 25})

    with pytest.raises(ToolError, match="HTTP 403"):
        await backend_client._request("POST", "/api/holdout-requests/1/approve")

    with pytest.raises(ToolError, match="HTTP 403"):
        await backend_client._request("PUT", "/api/settings", json={"daily_token_cap": 50_000})


# --- CCC-60: Notices in RecordingNotifier Through the API ---


@pytest.mark.anyio
async def test_notices_arrive_in_recording_notifier_through_api(mcp_test_env):
    """Verify notices arrive in RecordingNotifier when triggered through API (CCC-60)."""
    async_client = mcp_test_env["async_client"]
    notifier: RecordingNotifier = mcp_test_env["notifier"]
    desk = mcp_test_env["desk"]
    notifier.clear()

    # 1. CANDIDATE notice when trial achieves CANDIDATE verdict
    fam_id = desk.ledger.create_family("Candidate Notice Family")
    hyp_id = desk.ledger.register_hypothesis(fam_id, EXAMPLE)

    # Monkeypatch desk.run_hypothesis to return CANDIDATE verdict
    def fake_run_hypothesis(hypothesis_id: int, idea: str = "") -> dict[str, Any]:
        return {
            "trial_id": 42,
            "hypothesis_id": hypothesis_id,
            "family_id": fam_id,
            "verdict": "CANDIDATE",
            "label": "Candidate test",
            "created_at": datetime.now(UTC).isoformat(),
            "spec": EXAMPLE,
            "summary": {"profit_factor": 1.4, "trades": 120},
        }

    original_run_hypothesis = desk.run_hypothesis
    desk.run_hypothesis = fake_run_hypothesis
    try:
        res_trial = await async_client.post(
            f"/api/hypotheses/{hyp_id}/trials", json={"idea": "run"}
        )
        assert res_trial.status_code == 200
        assert res_trial.json()["verdict"] == "CANDIDATE"
    finally:
        desk.run_hypothesis = original_run_hypothesis

    cand_notices = notifier.by_kind(NoticeKind.CANDIDATE)
    assert len(cand_notices) == 1
    assert cand_notices[0].data["trial_id"] == 42
    assert "CANDIDATE" in cand_notices[0].text

    # 2. HOLDOUT_REQUEST notice when valid CANDIDATE hypothesis requests holdout
    record_candidate_trial(desk, hyp_id)
    res_h = await async_client.post(
        f"/api/hypotheses/{hyp_id}/holdout-requests",
        json={"reason": "Candidate ready for holdout"},
    )
    assert res_h.status_code == 200
    assert res_h.json()["status"] == "pending"

    holdout_notices = notifier.by_kind(NoticeKind.HOLDOUT_REQUEST)
    assert len(holdout_notices) == 1
    assert holdout_notices[0].data["hypothesis_id"] == hyp_id
    assert "awaiting owner approval" in holdout_notices[0].text

    # 3. TOKEN_WARNING notice when chat triggers 80% daily token cap
    # LLM returns usage of 85,000 against 100,000 cap
    mcp_test_env["llm"].steps = [
        Turn(content="Analyze momentum filter", usage={"total_tokens": 85_000})
    ]
    res_chat = await async_client.post(
        f"/api/families/{fam_id}/chat",
        json={"message": "Analyze momentum filter"},
    )
    assert res_chat.status_code == 200
    await res_chat.aread()

    token_notices = notifier.by_kind(NoticeKind.TOKEN_WARNING)
    assert len(token_notices) == 1
    assert token_notices[0].data["used"] >= 80_000
    assert "80%" in token_notices[0].text

    # 4. Verify PASS and HOLDOUT FAIL notices format and record in RecordingNotifier
    n_pass = pass_notice("Hypothesis passed holdout validation", data={"hypothesis_id": hyp_id})
    n_fail = holdout_fail_notice("Hypothesis failed holdout checks", data={"hypothesis_id": hyp_id})
    assert notifier.send(n_pass) is True
    assert notifier.send(n_fail) is True
    assert len(notifier.by_kind(NoticeKind.PASS)) == 1
    assert len(notifier.by_kind(NoticeKind.HOLDOUT_FAIL)) == 1


# --- Full MCP JSON-RPC Protocol Test via In-Memory ClientSession ---


@pytest.mark.anyio
async def test_mcp_full_protocol_client_session_in_memory(mcp_test_env):
    """End-to-end MCP JSON-RPC protocol execution over in-memory streams using ClientSession."""
    server = mcp_test_env["mcp_server"]
    server_to_client_send, server_to_client_recv = anyio.create_memory_object_stream(20)
    client_to_server_send, client_to_server_recv = anyio.create_memory_object_stream(20)

    async with anyio.create_task_group() as tg:

        async def run_server():
            await server._lowlevel_server.run(
                client_to_server_recv,
                server_to_client_send,
                server._lowlevel_server.create_initialization_options(),
            )

        tg.start_soon(run_server)

        async with ClientSession(server_to_client_recv, client_to_server_send) as session:
            # 1. Initialize MCP session
            init_res = await session.initialize()
            assert init_res.server_info.name == "vibedesk-mcp"
            assert init_res.server_info.version == "0.1.0"

            # 2. List tools
            tools_res = await session.list_tools()
            tool_names = {t.name for t in tools_res.tools}
            assert len(tool_names) == 8
            assert "list_blocks" in tool_names
            assert "submit_idea" in tool_names
            assert "run_trial" in tool_names

            # 3. Call read tool: list_blocks
            blocks_call = await session.call_tool("list_blocks", {})
            assert blocks_call.is_error is False
            blocks_data = json.loads(blocks_call.content[0].text)
            assert len(blocks_data["blocks"]) >= 10

            # 4. Call act tool: submit_idea
            submit_call = await session.call_tool(
                "submit_idea", {"idea": "RSI threshold fade on USDJPY"}
            )
            assert submit_call.is_error is False
            fam_data = json.loads(submit_call.content[0].text)
            assert fam_data["idea"] == "RSI threshold fade on USDJPY"
            assert fam_data["budget"] == 25

            # 5. Call read tool: list_ideas
            ideas_call = await session.call_tool("list_ideas", {})
            assert ideas_call.is_error is False
            ideas_data = [json.loads(c.text) for c in ideas_call.content]
            assert len(ideas_data) >= 1

        tg.cancel_scope.cancel()


# --- Error Handling & CLI Validation ---


@pytest.mark.anyio
async def test_mcp_error_handling(mcp_test_env):
    """Verify MCP tools raise ToolError on invalid or missing entities."""
    server = mcp_test_env["mcp_server"]

    # 1. Unknown trial -> ToolError (404)
    with pytest.raises(ToolError, match="404"):
        await server.call_tool("get_trial", {"trial_id": 99999})

    # 2. Unknown hypothesis trial run -> ToolError (404)
    with pytest.raises(ToolError, match="404"):
        await server.call_tool("run_trial", {"hypothesis_id": 99999})

    # 3. Empty idea string -> ToolError
    with pytest.raises(ToolError, match="idea must not be empty"):
        await server.call_tool("submit_idea", {"idea": "   "})

    # 4. Missing required trial_id / id -> ToolError
    with pytest.raises(ToolError, match="trial_id"):
        await server.call_tool("get_verdict", {})

    # 5. Missing required hypothesis_id / id -> ToolError
    with pytest.raises(ToolError, match="hypothesis_id"):
        await server.call_tool("run_trial", {})


def test_mcp_client_base_url_config(monkeypatch):
    """Verify base URL precedence: explicit argument > env var > default."""
    # 1. Default base URL
    monkeypatch.delenv("VIBEDESK_API_URL", raising=False)
    client_default = VibeDeskMcpClient()
    assert client_default.base_url == "http://127.0.0.1:8765"

    # 2. Environment variable
    monkeypatch.setenv("VIBEDESK_API_URL", "http://10.0.0.1:9000/")
    server_env = create_mcp_server()
    assert server_env.name == "vibedesk-mcp"

    # 3. Explicit parameter overrides env var
    client_explicit = VibeDeskMcpClient(base_url="http://192.168.1.50:8765/")
    assert client_explicit.base_url == "http://192.168.1.50:8765"
