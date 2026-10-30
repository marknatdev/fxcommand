"""Tests for HTTP API (spec §8.1, CCC-50, CCC-55).

Tests cover:
- All 15 endpoints in ENDPOINT_TABLE
- Owner cookie requirement (403 refused without cookie, 200 allowed with cookie)
- UI page setting the owner cookie with SameSite=Strict
- Family creation, retrieval, and 404s
- Hypothesis retrieval and trial runs
- Family chat SSE stream (404 for unknown family)
- Blocks library and Ledger stats
- Holdout requests and approval workflow
- Settings GET & PUT
- Health endpoint
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from vibedesk.app.api_spec import (
    ENDPOINT_TABLE,
    BlockLibraryOut,
    BudgetGrantOut,
    FamilyDetailOut,
    FamilySummaryOut,
    HealthOut,
    HoldoutRequestOut,
    HypothesisDetailOut,
    LedgerStatsOut,
    PublicSettingsOut,
    TrialSummaryOut,
)
from vibedesk.app.auth import OWNER_COOKIE_NAME, OWNER_COOKIE_VALUE, require_owner
from vibedesk.app.chats import Chats
from vibedesk.app.desk import Desk
from vibedesk.app.llm import ToolCall, Turn
from vibedesk.app.research import EXAMPLE
from vibedesk.app.server import create_app
from vibedesk.market.bars import Timeframe
from vibedesk.market.cache import record_import, write_chunk
from vibedesk.market.costs import CostMeasurement, record_cost
from vibedesk.market.fake_mt5 import FakeMt5
from vibedesk.market.synthetic import SyntheticBarSource
from vibedesk.notify import RecordingNotifier


class ScriptedLlm:
    def __init__(self, *replies: str) -> None:
        self.replies, self.calls = list(replies), []

    def chat(self, messages: list[dict]) -> str:
        self.calls.append(messages)
        if self.replies:
            return self.replies.pop(0)
        return "Scripted answer"


def reply(spec: dict, text: str = "A Donchian breakout on GOLD H1.") -> str:
    return f"{text}\n```json\n{json.dumps(spec)}\n```"


def setup_synthetic_gold_cache(root: Path) -> None:
    """Setup synthetic GOLD M15 and H1 bars with measured costs for tests."""
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


@pytest.fixture
def app_env(tmp_path: Path):
    data_dir = tmp_path / "data"
    settings_dir = tmp_path / "settings"
    data_dir.mkdir(parents=True, exist_ok=True)
    settings_dir.mkdir(parents=True, exist_ok=True)
    setup_synthetic_gold_cache(data_dir)

    notifier = RecordingNotifier()
    llm = ScriptedLlm(reply(EXAMPLE))
    app = create_app(
        root=data_dir,
        llm=llm,
        settings_root=settings_dir,
        notifier=notifier,
    )
    client = TestClient(app)
    return client, data_dir, settings_dir, notifier


def test_ui_page_sets_owner_cookie(app_env):
    client, _, _, _ = app_env
    res = client.get("/")
    assert res.status_code == 200
    set_cookie = res.headers.get("set-cookie", "")
    assert OWNER_COOKIE_NAME in set_cookie
    assert "samesite=strict" in set_cookie.lower()


def test_owner_only_endpoints_refused_without_cookie(app_env):
    client, data_dir, _, _ = app_env
    desk = Desk(data_dir)
    fam_id = desk.ledger.create_family("Owner Test Family")

    # 1. Extend budget without cookie -> 403
    res_b = client.post(f"/api/families/{fam_id}/budget", json={"amount": 25})
    assert res_b.status_code == 403
    assert "Owner action required" in res_b.json()["detail"]

    # 2. Approve holdout without cookie -> 403
    res_h = client.post("/api/holdout-requests/1/approve")
    assert res_h.status_code == 403
    assert "Owner action required" in res_h.json()["detail"]

    # 3. Update settings (PUT) without cookie -> 403
    res_s = client.put("/api/settings", json={"daily_token_cap": 200_000})
    assert res_s.status_code == 403
    assert "Owner action required" in res_s.json()["detail"]


def test_owner_only_endpoints_allowed_with_cookie(app_env):
    client, data_dir, _, _ = app_env
    desk = Desk(data_dir)
    fam_id = desk.ledger.create_family("Owner Allowed Family")
    client.cookies.set(OWNER_COOKIE_NAME, OWNER_COOKIE_VALUE)

    # 1. Extend budget with cookie -> 200
    res_b = client.post(f"/api/families/{fam_id}/budget", json={"amount": 50})
    assert res_b.status_code == 200
    b_data = BudgetGrantOut(**res_b.json())
    assert b_data.family_id == fam_id
    assert b_data.added == 50
    assert b_data.new_budget == 75  # default 25 + 50

    # 2. Update settings with cookie -> 200
    res_s = client.put("/api/settings", json={"daily_token_cap": 150_000})
    assert res_s.status_code == 200
    s_data = PublicSettingsOut(**res_s.json())
    assert s_data.daily_token_cap == 150_000


def test_families_api_endpoints(app_env):
    client, _, _, _ = app_env

    # Initial list is empty
    res_list = client.get("/api/families")
    assert res_list.status_code == 200
    assert res_list.json() == []

    # Create family with whitespace or empty idea -> 400
    assert client.post("/api/families", json={"idea": "   "}).status_code == 400

    # Create valid family -> 200
    res_create = client.post("/api/families", json={"idea": "GOLD Breakout Strategy"})
    assert res_create.status_code == 200
    fam = FamilyDetailOut(**res_create.json())
    assert fam.id > 0
    assert fam.idea == "GOLD Breakout Strategy"
    assert fam.budget == 25
    assert fam.trial_count == 0
    assert fam.hypotheses == []

    # Get family by id
    res_get = client.get(f"/api/families/{fam.id}")
    assert res_get.status_code == 200
    assert res_get.json()["id"] == fam.id

    # Unknown family -> 404
    assert client.get("/api/families/9999").status_code == 404

    # List now shows the family
    res_list2 = client.get("/api/families")
    assert len(res_list2.json()) == 1
    summary = FamilySummaryOut(**res_list2.json()[0])
    assert summary.id == fam.id
    assert summary.trial_count == 0


def test_hypotheses_and_trials_api_endpoints(app_env):
    client, data_dir, _, _ = app_env
    desk = Desk(data_dir)
    fam_id = desk.ledger.create_family("Hypothesis Family")
    hyp_id = desk.ledger.register_hypothesis(fam_id, EXAMPLE)

    # Unknown hypothesis -> 404
    assert client.get("/api/hypotheses/9999").status_code == 404

    # Get registered hypothesis -> 200
    res_hyp = client.get(f"/api/hypotheses/{hyp_id}")
    assert res_hyp.status_code == 200
    hyp_detail = HypothesisDetailOut(**res_hyp.json())
    assert hyp_detail.id == hyp_id
    assert hyp_detail.symbol == "GOLD"
    assert hyp_detail.entry_block == "donchian_breakout"
    assert hyp_detail.trial_ids == []

    # Run trial on unknown hypothesis -> 404
    assert client.post("/api/hypotheses/9999/trials", json={}).status_code == 404

    # Run trial on valid hypothesis -> 200
    res_trial = client.post(f"/api/hypotheses/{hyp_id}/trials", json={"idea": "Run 1"})
    assert res_trial.status_code == 200
    trial_data = TrialSummaryOut(**res_trial.json())
    assert trial_data.hypothesis_id == hyp_id
    assert trial_data.family_id == fam_id
    assert trial_data.verdict in ("CANDIDATE", "REJECT")
    assert trial_data.trial_id > 0

    # Get trial detail -> 200
    res_t_get = client.get(f"/api/trials/{trial_data.trial_id}")
    assert res_t_get.status_code == 200
    t_get_data = TrialSummaryOut(**res_t_get.json())
    assert t_get_data.trial_id == trial_data.trial_id

    # Unknown trial -> 404
    assert client.get("/api/trials/9999").status_code == 404

    # Hypothesis now lists this trial_id
    res_hyp2 = client.get(f"/api/hypotheses/{hyp_id}")
    assert res_hyp2.json()["trial_ids"] == [trial_data.trial_id]


def test_family_chat_sse_stream(app_env):
    client, data_dir, _, _ = app_env
    desk = Desk(data_dir)
    fam_id = desk.ledger.create_family("Chat Test Family")

    # Unknown family -> 404
    res_404 = client.post("/api/families/9999/chat", json={"message": "hello"})
    assert res_404.status_code == 404

    # Empty message -> 400
    res_400 = client.post(f"/api/families/{fam_id}/chat", json={"message": "   "})
    assert res_400.status_code == 400

    # Valid family -> 200 event-stream
    res_stream = client.post(f"/api/families/{fam_id}/chat", json={"message": "trade breakouts"})
    assert res_stream.status_code == 200
    assert "text/event-stream" in res_stream.headers["content-type"]
    text = res_stream.text
    assert "event: message" in text or "event: tool_started" in text


def test_blocks_and_ledger_endpoints(app_env):
    client, _, _, _ = app_env

    # Blocks endpoint
    res_blocks = client.get("/api/blocks")
    assert res_blocks.status_code == 200
    blocks = BlockLibraryOut(**res_blocks.json())
    assert len(blocks.blocks) >= 10
    block_names = {b.name for b in blocks.blocks}
    assert "donchian_breakout" in block_names
    assert "atr_stop" in block_names

    # Ledger endpoint
    res_ledger = client.get("/api/ledger")
    assert res_ledger.status_code == 200
    ledger_stats = LedgerStatsOut(**res_ledger.json())
    assert ledger_stats.desk_trial_count >= 0
    assert isinstance(ledger_stats.families, list)


def test_holdout_request_workflow(app_env):
    client, data_dir, _, _ = app_env
    desk = Desk(data_dir)
    fam_id = desk.ledger.create_family("Holdout Family")
    hyp_id = desk.ledger.register_hypothesis(fam_id, EXAMPLE)

    # Submitting holdout request for hypothesis with no CANDIDATE returns refused status
    res_req = client.post(
        f"/api/hypotheses/{hyp_id}/holdout-requests", json={"reason": "Test reason"}
    )
    assert res_req.status_code == 200
    h_out = HoldoutRequestOut(**res_req.json())
    assert h_out.status == "refused"
    assert h_out.refusal_reason is not None

    # Unknown hypothesis -> 404
    assert (
        client.post("/api/hypotheses/9999/holdout-requests", json={"reason": ""}).status_code == 404
    )

    # Unknown approve request -> 404
    client.cookies.set(OWNER_COOKIE_NAME, OWNER_COOKIE_VALUE)
    assert client.post("/api/holdout-requests/9999/approve").status_code == 404

    # GET /api/holdout-requests: read-only, no owner cookie needed
    # Lists requests with optional status filter
    client.cookies.clear()
    req_id = desk.ledger.request_holdout(hyp_id)
    res_list = client.get("/api/holdout-requests")
    assert res_list.status_code == 200
    items = res_list.json()
    assert isinstance(items, list)
    item = next(x for x in items if x["id"] == req_id)
    assert item["hypothesis_id"] == hyp_id
    assert item["family_id"] == fam_id
    assert item["status"] == "pending"
    assert "trial_id" in item
    assert "created_at" in item

    # Filter by status=pending and status=approved
    res_pending = client.get("/api/holdout-requests?status=pending")
    assert res_pending.status_code == 200
    assert any(x["id"] == req_id for x in res_pending.json())

    res_approved = client.get("/api/holdout-requests?status=approved")
    assert res_approved.status_code == 200
    assert not any(x["id"] == req_id for x in res_approved.json())


def test_settings_get_and_put(app_env):
    client, _, _, _ = app_env

    # GET settings returns public fields
    res_get = client.get("/api/settings")
    assert res_get.status_code == 200
    pub = PublicSettingsOut(**res_get.json())
    assert pub.daily_token_cap == 100_000
    assert hasattr(pub, "telegram_enabled")
    assert hasattr(pub, "llm_key_set")

    # PUT settings updates values (owner-only)
    client.cookies.set(OWNER_COOKIE_NAME, OWNER_COOKIE_VALUE)
    res_put = client.put(
        "/api/settings",
        json={"daily_token_cap": 250_000, "telegram_enabled": True},
    )
    assert res_put.status_code == 200
    pub_updated = PublicSettingsOut(**res_put.json())
    assert pub_updated.daily_token_cap == 250_000
    assert pub_updated.telegram_enabled is True
    assert client.app.state.budget.daily_cap == 250_000
    assert client.app.state.scheduler.notifier is client.app.state.notifier


def test_health_endpoint(tmp_path: Path):
    from vibedesk.market.fake_mt5 import FakeMt5

    data_dir = tmp_path / "data"
    settings_dir = tmp_path / "settings"
    data_dir.mkdir(parents=True, exist_ok=True)
    settings_dir.mkdir(parents=True, exist_ok=True)
    setup_synthetic_gold_cache(data_dir)

    fake_api = FakeMt5(SyntheticBarSource(1, date(2013, 12, 1), date(2027, 1, 1)))
    app = create_app(
        root=data_dir,
        llm=ScriptedLlm(reply(EXAMPLE)),
        settings_root=settings_dir,
        mt5_api=fake_api,
        enable_scheduler=True,
    )
    with TestClient(app) as client:
        res_health = client.get("/api/health")
        assert res_health.status_code == 200
        health = HealthOut(**res_health.json())
        assert health.token_budget_state in ("ok", "warn", "paused")
        assert isinstance(health.tokens_used_today, int)
        assert isinstance(health.active_jobs, int)
        assert health.mt5_connected is True
        assert health.omniroute_connected is True


def test_real_launcher_smoke_all_get_endpoints(tmp_path: Path):
    """Smoke test matching the real launcher (create_app with real Scheduler, real TokenBudget,
    lifespan active) calling every GET endpoint in ENDPOINT_TABLE + static UI, asserting no 5xx."""
    from vibedesk.app.scheduler import Scheduler
    from vibedesk.desk.budget import TokenBudget
    from vibedesk.market.fake_mt5 import FakeMt5

    data_dir = tmp_path / "data"
    settings_dir = tmp_path / "settings"
    data_dir.mkdir(parents=True, exist_ok=True)
    settings_dir.mkdir(parents=True, exist_ok=True)
    setup_synthetic_gold_cache(data_dir)

    # Seed data: Family, Hypothesis, Trial, HoldoutRequest, Chat
    desk = Desk(data_dir)
    fam_id = desk.ledger.create_family("Real Launcher Smoke Family")
    hyp_id = desk.ledger.register_hypothesis(fam_id, EXAMPLE)
    trial_res = desk.run_hypothesis(hyp_id, idea="Smoke Trial")
    trial_id = trial_res["trial_id"]
    req_id = desk.ledger.request_holdout(hyp_id)
    chats = Chats(data_dir / "chats.db")
    chat_id = chats.create("Smoke Chat")
    chats.append(chat_id, {"role": "user", "text": "hello"}, {"role": "assistant", "text": "hi"})

    fake_api = FakeMt5(SyntheticBarSource(1, date(2013, 12, 1), date(2027, 1, 1)))

    # Real launcher creates app with default enable_scheduler=True,
    # building real Scheduler & TokenBudget
    app = create_app(
        root=data_dir,
        db=data_dir / "desk.db",
        settings_root=settings_dir,
        llm=ScriptedLlm(reply(EXAMPLE)),
        mt5_api=fake_api,
        notifier=RecordingNotifier(),
    )

    with TestClient(app) as client:
        assert isinstance(app.state.scheduler, Scheduler)
        assert app.state.scheduler.is_running is True
        assert isinstance(app.state.budget, TokenBudget)

        # Static page
        res_ui = client.get("/")
        assert res_ui.status_code == 200
        assert "text/html" in res_ui.headers.get("content-type", "")
        assert len(res_ui.text) > 0

        # Every GET endpoint in ENDPOINT_TABLE
        id_map = {
            "families": fam_id,
            "hypotheses": hyp_id,
            "trials": trial_id,
        }
        get_endpoints = [entry for entry in ENDPOINT_TABLE if entry["method"] == "GET"]
        assert len(get_endpoints) >= 8

        for entry in get_endpoints:
            endpoint_path = entry["path"]
            for resource, rid in id_map.items():
                if f"/api/{resource}/{{id}}" == endpoint_path:
                    endpoint_path = f"/api/{resource}/{rid}"
                    break
            res = client.get(endpoint_path)
            assert res.status_code < 500, f"{entry['path']} returned {res.status_code}: {res.text}"
            assert res.status_code == 200, f"{entry['path']} expected 200, got {res.status_code}"

        # Holdout requests endpoint includes seeded req_id
        res_holdout = client.get("/api/holdout-requests")
        assert any(x["id"] == req_id for x in res_holdout.json())

        # Additional GET endpoints in simple app
        extra_get = [
            "/api/status",
            "/api/models",
            "/api/chats",
            f"/api/chats/{chat_id}",
            "/api/trials",
        ]
        for extra_path in extra_get:
            res = client.get(extra_path)
            assert res.status_code < 500, f"{extra_path} returned {res.status_code}: {res.text}"
            assert res.status_code == 200, f"{extra_path} expected 200, got {res.status_code}"

        # Verify /api/health specifically
        res_health = client.get("/api/health")
        assert res_health.status_code == 200
        health = HealthOut(**res_health.json())
        assert health.mt5_connected is True
        assert health.active_jobs >= 0

    # Also test without MT5 (mt5_api=None) and NoOp notifier
    # like default Start Vibe Desk without MT5
    app_no_mt5 = create_app(
        root=data_dir,
        db=data_dir / "desk.db",
        settings_root=settings_dir,
        llm=ScriptedLlm(reply(EXAMPLE)),
        mt5_api=None,
    )
    with TestClient(app_no_mt5) as client:
        assert isinstance(app_no_mt5.state.scheduler, Scheduler)
        assert app_no_mt5.state.scheduler.is_running is True
        res_health_no_mt5 = client.get("/api/health")
        assert res_health_no_mt5.status_code == 200
        health_no_mt5 = HealthOut(**res_health_no_mt5.json())
        assert health_no_mt5.token_budget_state in ("ok", "warn", "paused")


def test_all_15_endpoints_in_table_covered(app_env):
    """Build the app and assert every (method, path) in ENDPOINT_TABLE exists in app.routes,
    and that owner_only routes carry require_owner."""
    client, _, _, _ = app_env
    app = client.app

    route_map: dict[tuple[str, str], APIRoute] = {}
    for route in app.routes:
        if isinstance(route, APIRoute):
            norm_path = re.sub(r"\{[a-z_]*id\}", "{id}", route.path)
            for m in route.methods:
                route_map[(m.upper(), norm_path)] = route

    assert len(ENDPOINT_TABLE) >= 15
    for ep in ENDPOINT_TABLE:
        key = (ep["method"].upper(), ep["path"])
        assert key in route_map, f"Missing route in app: {key}"
        route = route_map[key]
        if ep.get("owner_only"):
            dep_fns = [d.dependency for d in route.dependencies]
            assert require_owner in dep_fns, f"Route {key} must have require_owner dependency"


def test_chat_stream_starter_and_desk_tools_wiring(app_env):
    client, data_dir, _, _ = app_env
    desk = Desk(data_dir)
    fam_id = desk.ledger.create_family("Wiring Test Family")

    # 1. POST /api/chat with empty message -> 400
    res_400 = client.post("/api/chat", json={"message": "   "})
    assert res_400.status_code == 400

    # 2. POST /api/chat with unknown family_id -> 404
    res_404 = client.post("/api/chat", json={"message": "breakout", "family_id": 9999})
    assert res_404.status_code == 404

    # 3. POST /api/chat without family_id -> 200 (uses starter_tools)
    res_starter = client.post("/api/chat", json={"message": "what blocks exist?"})
    assert res_starter.status_code == 200
    assert "text/event-stream" in res_starter.headers["content-type"]
    assert "event: message" in res_starter.text

    # 4. POST /api/chat with valid family_id -> 200 (builds DeskContext and desk tools)
    res_fam = client.post("/api/chat", json={"message": "breakout idea", "family_id": fam_id})
    assert res_fam.status_code == 200
    assert "text/event-stream" in res_fam.headers["content-type"]
    assert "event: message" in res_fam.text

    # 5. POST /api/chat with chat_id resolving family_id from prior trial
    hyp_id = desk.ledger.register_hypothesis(fam_id, EXAMPLE)
    res_trial = client.post(f"/api/hypotheses/{hyp_id}/trials", json={"idea": "Run 1"})
    trial_id = res_trial.json()["trial_id"]
    chats = Chats(data_dir / "chats.db")
    chat_id = chats.create("Chat with trial")
    chats.append(
        chat_id,
        {"role": "user", "text": "start"},
        {"role": "trial", "trial_id": trial_id, "text": "done"},
    )
    res_chat_id = client.post("/api/chat", json={"message": "continue", "chat_id": chat_id})
    assert res_chat_id.status_code == 200
    assert "text/event-stream" in res_chat_id.headers["content-type"]

    # 6. Shared TokenBudget updates on PUT /api/settings
    client.cookies.set(OWNER_COOKIE_NAME, OWNER_COOKIE_VALUE)
    res_put = client.put("/api/settings", json={"daily_token_cap": 300_000})
    assert res_put.status_code == 200
    assert client.app.state.budget.daily_cap == 300_000


def test_fresh_recording_notifier_preserved_in_create_app(tmp_path: Path):
    """An empty RecordingNotifier (len == 0, falsy) passed to create_app must NOT be replaced."""
    fresh_notifier = RecordingNotifier()
    assert len(fresh_notifier) == 0
    assert not fresh_notifier

    app = create_app(
        root=tmp_path / "data",
        settings_root=tmp_path / "settings",
        notifier=fresh_notifier,
        enable_scheduler=True,
    )
    assert app.state.notifier is fresh_notifier
    assert app.state.scheduler.notifier is fresh_notifier


class _E2EDeskLLM:
    """Scripted LLM for M4 E2E that extracts hypothesis_id from tool responses."""

    def __init__(self, family_id: int) -> None:
        self.family_id = family_id
        self.step = 0
        self.recorded_hyp_id: int | None = None

    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> Turn:
        self.step += 1
        if self.step == 1:
            return Turn(
                tool_calls=(
                    ToolCall("register_hypothesis", {"spec": EXAMPLE, "family_id": self.family_id}),
                ),
                usage={"total_tokens": 150},
            )
        if self.step == 2:
            tool_msg = messages[-1]
            data = json.loads(tool_msg["content"])
            self.recorded_hyp_id = int(data["hypothesis_id"])
            return Turn(
                tool_calls=(ToolCall("run_trial", {"hypothesis_id": self.recorded_hyp_id}),),
                usage={"total_tokens": 250},
            )
        return Turn(
            content="The hypothesis was evaluated and rejected on the Research Window.",
            usage={"total_tokens": 75},
        )


def _setup_e2e_harness(tmp_path: Path) -> tuple[Desk, int, TestClient, _E2EDeskLLM]:
    data_dir = tmp_path / "data"
    settings_dir = tmp_path / "settings"
    data_dir.mkdir(parents=True, exist_ok=True)
    settings_dir.mkdir(parents=True, exist_ok=True)
    setup_synthetic_gold_cache(data_dir)

    desk = Desk(data_dir)
    fam_id = desk.ledger.create_family("M4 E2E Family")
    llm = _E2EDeskLLM(fam_id)
    fake_api = FakeMt5(SyntheticBarSource(1, date(2013, 12, 1), date(2027, 1, 1)))

    app = create_app(
        root=data_dir,
        llm=llm,
        settings_root=settings_dir,
        notifier=RecordingNotifier(),
        mt5_api=fake_api,
        enable_scheduler=False,
    )
    return desk, fam_id, TestClient(app), llm


def test_e2e_m4_idea_to_verdict_via_family_chat(tmp_path: Path):
    """End-to-end M4 done-when test (spec §10, CCC-44..CCC-55):
    A scripted idea through POST /api/families/{id}/chat dynamically
    registers a Hypothesis, runs a Trial, and yields a verdict event.
    """
    desk, fam_id, client, llm = _setup_e2e_harness(tmp_path)

    res = client.post(
        f"/api/families/{fam_id}/chat",
        json={"message": "Test a GOLD H1 breakout idea"},
    )
    assert res.status_code == 200
    assert "text/event-stream" in res.headers["content-type"]
    text = res.text

    assert "event: verdict" in text
    assert "event: tool_started" in text

    # Verify Trial recorded with the dynamically registered hypothesis_id
    assert llm.recorded_hyp_id is not None
    trials = desk.ledger.trials_for(llm.recorded_hyp_id)
    assert len(trials) == 1
    assert trials[0].hypothesis_id == llm.recorded_hyp_id
    assert trials[0].verdict in ("CANDIDATE", "REJECT")

    # Verify token budget usage
    assert client.app.state.budget.used == 150 + 250 + 75
