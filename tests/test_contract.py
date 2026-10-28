"""Tests for Layer A Wave 0 contract interfaces and implementations."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from starlette.responses import Response

from vibedesk.app.api_spec import ENDPOINT_TABLE, MCP_TOOL_MAP
from vibedesk.app.auth import OWNER_COOKIE_NAME, OWNER_COOKIE_VALUE, require_owner, set_owner_cookie
from vibedesk.app.scheduler import Scheduler
from vibedesk.app.settings import Settings, load, load_secrets, save_secrets
from vibedesk.desk.budget import BudgetState, TokenBudget
from vibedesk.desk.tools import DeskContext, build_desk_tools
from vibedesk.notify import (
    NoOpNotifier,
    Notice,
    NoticeKind,
    RecordingNotifier,
    TelegramNotifier,
    get_notifier,
)

# --- Notify Tests ---


def test_recording_notifier_records_and_filters():
    notifier = RecordingNotifier()
    assert len(notifier) == 0
    assert notifier.last is None

    n1 = Notice(kind=NoticeKind.CANDIDATE, text="GOLD breakout looks real", data={"trial_id": 1})
    n2 = Notice(
        kind=NoticeKind.TOKEN_WARNING, text="80% daily budget reached", data={"used": 80000}
    )

    notifier.send(n1)
    notifier.send(n2)

    assert len(notifier) == 2
    assert notifier.last == n2
    assert notifier.by_kind(NoticeKind.CANDIDATE) == [n1]
    assert notifier.by_kind("token_warning") == [n2]

    notifier.clear()
    assert len(notifier) == 0


def test_telegram_notifier_stub_and_get_notifier(tmp_path: Path):
    with pytest.raises(ValueError, match="requires bot_token and chat_id"):
        TelegramNotifier("", "")

    tn = TelegramNotifier(bot_token="test_token", chat_id="12345")
    with pytest.raises(NotImplementedError, match="CCC-59"):
        tn.send(Notice(kind=NoticeKind.CANDIDATE, text="test"))

    # When no secrets exist, get_notifier falls back to NoOpNotifier
    fallback = get_notifier(root=tmp_path)
    assert isinstance(fallback, NoOpNotifier)

    # When secrets and settings are present and enabled
    save_secrets({"telegram_bot_token": "secret_token"}, root=tmp_path)
    s = Settings(telegram_enabled=True, telegram_chat_id="12345")
    wired = get_notifier(root=tmp_path, settings=s)
    assert isinstance(wired, TelegramNotifier)
    assert wired.bot_token == "secret_token"
    assert wired.chat_id == "12345"


# --- TokenBudget Tests ---


def test_token_budget_tracking_and_state():
    budget = TokenBudget(daily_cap=1000)
    assert budget.state() == BudgetState.OK
    assert not budget.is_paused
    assert not budget.needs_warning

    # Record 500 tokens (50%)
    budget.record_usage(500)
    assert budget.used == 500
    assert budget.remaining == 500
    assert budget.state() == BudgetState.OK

    # Record another 300 tokens (800 total = 80%)
    budget.record_usage({"total_tokens": 300})
    assert budget.used == 800
    assert budget.state() == BudgetState.WARN
    assert budget.needs_warning
    budget.mark_warned()
    assert not budget.needs_warning  # Only warns once per day

    # Record another 200 tokens (1000 total = 100%)
    budget.record_usage(200)
    assert budget.used == 1000
    assert budget.state() == BudgetState.PAUSED
    assert budget.is_paused

    # Status dictionary
    status = budget.status()
    assert status["daily_cap"] == 1000
    assert status["tokens_used"] == 1000
    assert status["paused"] is True
    assert status["state"] == "paused"


def test_token_budget_resets_on_bangkok_day_boundary():
    current_time = datetime(2026, 10, 24, 12, 0, tzinfo=ZoneInfo("Asia/Bangkok"))

    budget = TokenBudget(daily_cap=1000, now=lambda: current_time)
    budget.record_usage(1000)
    assert budget.state() == BudgetState.PAUSED
    assert budget.used == 1000

    # Moving to next Bangkok day resets the budget
    current_time = datetime(2026, 10, 25, 1, 0, tzinfo=ZoneInfo("Asia/Bangkok"))
    assert budget.state() == BudgetState.OK
    assert budget.remaining == 1000
    assert budget.used == 0

    # Moving clock backwards does NOT reset
    budget.record_usage(500)
    assert budget.used == 500
    current_time = datetime(2026, 10, 24, 12, 0, tzinfo=ZoneInfo("Asia/Bangkok"))
    assert budget.state() == BudgetState.OK
    assert budget.used == 500


# --- Desk Tools Tests ---


def test_desk_tools_registry_and_schemas():
    ctx = DeskContext(ledger=None, market=None, family_id=1)
    tools = build_desk_tools(ctx)

    expected_tools = {
        "list_blocks",
        "describe_data",
        "draft_spec",
        "register_hypothesis",
        "run_trial",
        "get_trial",
        "request_holdout",
    }
    assert {t.name for t in tools.all()} == expected_tools

    # list_blocks executes
    lb_tool = tools.get("list_blocks")
    assert lb_tool is not None
    blocks = lb_tool.fn()
    assert len(blocks["blocks"]) >= 10

    # draft_spec validates
    ds_tool = tools.get("draft_spec")
    assert ds_tool is not None
    bad_spec = ds_tool.fn({"name": "incomplete"})
    assert bad_spec["valid"] is False

    # Unimplemented tools raise NotImplementedError for Wave 1 Desk team
    for name in (
        "describe_data",
        "register_hypothesis",
        "run_trial",
        "get_trial",
        "request_holdout",
    ):
        tool = tools.get(name)
        assert tool is not None
        with pytest.raises(NotImplementedError, match="CCC-44"):
            if name in ("run_trial", "get_trial", "request_holdout"):
                tool.fn(1)
            elif name == "describe_data":
                tool.fn("GOLD", "M15")
            elif name == "register_hypothesis":
                tool.fn({})


# --- Auth Tests ---


def test_owner_auth_cookie():
    res = Response()
    set_owner_cookie(res)
    cookie_header = res.headers.get("set-cookie", "")
    assert OWNER_COOKIE_NAME in cookie_header
    assert "samesite=strict" in cookie_header.lower()

    # require_owner rejects missing cookie
    with pytest.raises(HTTPException) as exc_missing:
        require_owner(owner=None)
    assert exc_missing.value.status_code == 403

    # require_owner rejects wrong cookie value
    with pytest.raises(HTTPException) as exc_wrong:
        require_owner(owner="wrong_value")
    assert exc_wrong.value.status_code == 403

    # require_owner rejects empty cookie
    with pytest.raises(HTTPException) as exc_empty:
        require_owner(owner="")
    assert exc_empty.value.status_code == 403

    # require_owner accepts valid cookie
    assert require_owner(owner=OWNER_COOKIE_VALUE) is True


# --- Scheduler Tests ---


def test_scheduler_jobs_and_lifecycle(tmp_path: Path):
    scheduler = Scheduler(root=tmp_path)
    assert not scheduler.is_running
    jobs = scheduler.jobs()
    job_names = {j.name for j in jobs}
    assert job_names == {"nightly_refresh", "weekly_spread_floors", "daily_digest"}

    scheduler.start()
    assert scheduler.is_running
    scheduler.stop()
    assert not scheduler.is_running

    with pytest.raises(KeyError):
        scheduler.run_job_now("nonexistent_job")

    with pytest.raises(NotImplementedError, match="CCC-53"):
        scheduler.run_job_now("nightly_refresh")


# --- API Spec & MCP Tool Map Tests ---


def test_api_spec_and_mcp_map():
    assert len(ENDPOINT_TABLE) >= 15
    owner_paths = {ep["path"] for ep in ENDPOINT_TABLE if ep["owner_only"]}
    assert "/api/holdout-requests/{id}/approve" in owner_paths
    assert "/api/families/{id}/budget" in owner_paths
    assert "/api/settings" in owner_paths

    assert len(MCP_TOOL_MAP) == 8
    # Ensure MCP tool map only contains read and act, no owner paths
    for _tool_name, meta in MCP_TOOL_MAP.items():
        assert meta["kind"] in ("read", "act")
        assert meta["path"] not in owner_paths


# --- Settings & Secrets Tests ---


def test_settings_and_secrets(tmp_path: Path):
    s = load(root=tmp_path, env={})
    assert s.daily_token_cap == 100_000
    pub = s.public()
    assert "daily_token_cap" in pub
    assert "telegram_enabled" in pub
    assert "telegram_chat_id_set" in pub
    assert "llm_key" not in pub

    save_secrets({"telegram_bot_token": "tg123"}, root=tmp_path)
    sec = load_secrets(root=tmp_path)
    assert sec.get("telegram_bot_token") == "tg123"

    # Precedence: settings.json with empty key does NOT clobber environment key
    (tmp_path / "settings.json").write_text(
        '{"llm_key": "", "llm_url": "", "telegram_chat_id": ""}',
        encoding="utf-8",
    )
    s2 = load(
        root=tmp_path,
        env={
            "VIBEDESK_LLM_KEY": "sk-env-key",
            "VIBEDESK_LLM_URL": "http://127.0.0.1:20128/v1",
            "VIBEDESK_TELEGRAM_CHAT_ID": "chat-999",
        },
    )
    assert s2.llm_key == "sk-env-key"
    assert s2.llm_url == "http://127.0.0.1:20128/v1"
    assert s2.telegram_chat_id == "chat-999"

    # Boolean precedence: env telegram enabled stays True even if settings.json is false
    (tmp_path / "settings.json").write_text('{"telegram_enabled": false}', encoding="utf-8")
    s3 = load(root=tmp_path, env={"VIBEDESK_TELEGRAM_ENABLED": "1"})
    assert s3.telegram_enabled is True

    # Boolean precedence: settings.json enables telegram if env doesn't
    (tmp_path / "settings.json").write_text('{"telegram_enabled": true}', encoding="utf-8")
    s4 = load(root=tmp_path, env={})
    assert s4.telegram_enabled is True
