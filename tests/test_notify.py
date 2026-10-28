"""Tests for Telegram notices, daily digest, and notifiers (spec §8.4, CCC-59, CCC-60)."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import httpx
import pytest

from vibedesk.app.settings import Settings, save_secrets
from vibedesk.desk.budget import TokenBudget
from vibedesk.ledger.store import Ledger
from vibedesk.notify import (
    NoOpNotifier,
    Notice,
    NoticeKind,
    RecordingNotifier,
    TelegramNotifier,
    candidate_notice,
    compile_daily_digest,
    format_daily_digest,
    format_notice,
    get_notifier,
    holdout_fail_notice,
    holdout_request_notice,
    pass_notice,
    token_warning_notice,
)

# --- RecordingNotifier & NoOpNotifier Tests ---


def test_recording_notifier():
    notifier = RecordingNotifier()
    assert len(notifier) == 0
    assert notifier.last is None

    n1 = candidate_notice("GOLD breakout is CANDIDATE", data={"trial_id": 10})
    n2 = token_warning_notice("80% budget reached", data={"used": 80000})

    assert notifier.send(n1) is True
    assert notifier.send(n2) is True

    assert len(notifier) == 2
    assert notifier.last == n2
    assert notifier.by_kind(NoticeKind.CANDIDATE) == [n1]
    assert notifier.by_kind(NoticeKind.TOKEN_WARNING) == [n2]
    assert notifier.by_kind("candidate") == [n1]
    assert notifier.by_kind(NoticeKind.PASS) == []

    notifier.clear()
    assert len(notifier) == 0
    assert notifier.last is None


def test_noop_notifier():
    notifier = NoOpNotifier()
    notice = candidate_notice("noop check")
    assert notifier.send(notice) is True


# --- Format Notices & Helpers Tests ---


def test_format_notice_prefixes():
    n_cand = Notice(kind=NoticeKind.CANDIDATE, text="GOLD H1 breakout")
    assert format_notice(n_cand) == "[CANDIDATE] GOLD H1 breakout"

    n_pass = Notice(kind=NoticeKind.PASS, text="EURUSD H4 trend")
    assert format_notice(n_pass) == "[PASS] EURUSD H4 trend"

    n_fail = Notice(kind=NoticeKind.HOLDOUT_FAIL, text="USDCAD M15 bounce")
    assert format_notice(n_fail) == "[HOLDOUT FAIL] USDCAD M15 bounce"

    n_req = Notice(kind=NoticeKind.HOLDOUT_REQUEST, text="Request #5 awaiting approval")
    assert format_notice(n_req) == "[HOLDOUT REQUEST] Request #5 awaiting approval"

    n_warn = Notice(kind=NoticeKind.TOKEN_WARNING, text="80% daily budget reached")
    assert format_notice(n_warn) == "[TOKEN BUDGET WARNING] 80% daily budget reached"

    n_digest = Notice(kind=NoticeKind.DAILY_DIGEST, text="Trials run: 10\nREJECT count: 8")
    assert format_notice(n_digest) == "[DAILY DIGEST] Trials run: 10\nREJECT count: 8"

    # Does not duplicate prefix if already present
    already_prefixed = Notice(kind=NoticeKind.CANDIDATE, text="[CANDIDATE] GOLD H1 breakout")
    assert format_notice(already_prefixed) == "[CANDIDATE] GOLD H1 breakout"


def test_notice_helper_constructors():
    c = candidate_notice("cand text", data={"id": 1})
    assert c.kind == NoticeKind.CANDIDATE
    assert c.text == "cand text"
    assert c.data == {"id": 1}

    p = pass_notice("pass text", data={"id": 2})
    assert p.kind == NoticeKind.PASS
    assert p.text == "pass text"

    hf = holdout_fail_notice("fail text", data={"id": 3})
    assert hf.kind == NoticeKind.HOLDOUT_FAIL
    assert hf.text == "fail text"

    hr = holdout_request_notice("req text", data={"id": 4})
    assert hr.kind == NoticeKind.HOLDOUT_REQUEST
    assert hr.text == "req text"

    tw = token_warning_notice("warn text", data={"used": 85000})
    assert tw.kind == NoticeKind.TOKEN_WARNING
    assert tw.text == "warn text"


# --- Daily Digest Formatting & Compilation Tests ---


def test_format_daily_digest_text():
    text = format_daily_digest(
        reject_count=15,
        trials_run=20,
        tokens_used=75000,
        date="2026-10-28",
    )
    assert "Daily Digest (2026-10-28)" in text
    assert "- Trials run: 20" in text
    assert "- REJECT count: 15" in text
    assert "- Tokens used: 75000" in text


def test_compile_daily_digest_explicit_counts():
    notice = compile_daily_digest(
        reject_count=5,
        trials_run=6,
        tokens_used=12000,
        date="2026-10-28",
    )
    assert notice.kind == NoticeKind.DAILY_DIGEST
    assert "Trials run: 6" in notice.text
    assert "REJECT count: 5" in notice.text
    assert "Tokens used: 12000" in notice.text
    assert notice.data["reject_count"] == 5
    assert notice.data["trials_run"] == 6
    assert notice.data["tokens_used"] == 12000


def test_compile_daily_digest_with_budget_and_ledger(tmp_path: Path):
    budget = TokenBudget(daily_cap=100000)
    budget.record_usage(42000)

    ledger_path = tmp_path / "desk.db"
    ledger = Ledger(ledger_path)

    # Compile digest with empty ledger
    notice_empty = compile_daily_digest(budget=budget, ledger=ledger, date="2026-10-28")
    assert notice_empty.data["tokens_used"] == 42000
    assert notice_empty.data["trials_run"] == 0
    assert notice_empty.data["reject_count"] == 0

    ledger.close()


# --- TelegramNotifier Push Delivery & Security Tests ---


def test_telegram_notifier_validation():
    with pytest.raises(ValueError, match="requires bot_token and chat_id"):
        TelegramNotifier(bot_token="", chat_id="123")
    with pytest.raises(ValueError, match="requires bot_token and chat_id"):
        TelegramNotifier(bot_token="tok", chat_id="")


def test_telegram_notifier_repr_redacts_token():
    notifier = TelegramNotifier(bot_token="very-secret-token-xyz", chat_id="45678")
    r = repr(notifier)
    assert "very-secret-token-xyz" not in r
    assert "[REDACTED]" in r
    assert "45678" in r


def test_telegram_notifier_pushes_all_spec_notice_types():
    sent_requests: list[httpx.Request] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        sent_requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 101}})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    bot_token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    chat_id = "987654321"

    notifier = TelegramNotifier(
        bot_token=bot_token,
        chat_id=chat_id,
        base_url="https://api.telegram.org",
        client=client,
    )

    test_notices = [
        candidate_notice("GOLD breakout CANDIDATE", data={"trial_id": 1}),
        pass_notice("EURUSD trend PASS", data={"trial_id": 2}),
        holdout_fail_notice("USDJPY mean reversion HOLDOUT FAIL", data={"trial_id": 3}),
        holdout_request_notice("Holdout Request awaiting approval", data={"request_id": 4}),
        token_warning_notice("Token budget at 80%", data={"used": 80000}),
        compile_daily_digest(reject_count=7, trials_run=8, tokens_used=50000),
    ]

    for notice in test_notices:
        delivered = notifier.send(notice)
        assert delivered is True

    assert len(sent_requests) == 6

    # Verify all requests targeted the Telegram sendMessage endpoint
    for req in sent_requests:
        assert req.method == "POST"
        assert req.url.path == f"/bot{bot_token}/sendMessage"
        body = json.loads(req.content.decode("utf-8"))
        assert body["chat_id"] == chat_id
        assert "text" in body

    # Inspect notice prefixes in body
    expected_prefixes = [
        "[CANDIDATE]",
        "[PASS]",
        "[HOLDOUT FAIL]",
        "[HOLDOUT REQUEST]",
        "[TOKEN BUDGET WARNING]",
        "[DAILY DIGEST]",
    ]
    for req, exp_prefix in zip(sent_requests, expected_prefixes, strict=True):
        body = json.loads(req.content.decode("utf-8"))
        assert exp_prefix in body["text"]


def test_telegram_notifier_handles_api_rejection(caplog: pytest.LogCaptureFixture):
    def mock_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked"},
        )

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    bot_token = "secret-bot-tok"
    notifier = TelegramNotifier(bot_token=bot_token, chat_id="12345", client=client)

    with caplog.at_level(logging.WARNING):
        result = notifier.send(candidate_notice("test notice"))

    assert result is False
    assert bot_token not in caplog.text
    assert "Forbidden: bot was blocked" in caplog.text


def test_telegram_notifier_handles_http_status_error(caplog: pytest.LogCaptureFixture):
    def mock_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error: secret-bot-tok leaked by server")

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    bot_token = "secret-bot-tok"
    notifier = TelegramNotifier(bot_token=bot_token, chat_id="12345", client=client)

    with caplog.at_level(logging.WARNING):
        result = notifier.send(candidate_notice("test notice"))

    assert result is False
    # Ensure bot_token was redacted even if server echo reflected it
    assert bot_token not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_telegram_notifier_handles_network_exception(caplog: pytest.LogCaptureFixture):
    def mock_handler(request: httpx.Request) -> httpx.Response:
        # Simulate connection error whose URL contains the bot token
        raise httpx.ConnectError(f"Connection failed to {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    bot_token = "secret-bot-tok-999"
    notifier = TelegramNotifier(bot_token=bot_token, chat_id="12345", client=client)

    with caplog.at_level(logging.WARNING):
        result = notifier.send(candidate_notice("test notice"))

    assert result is False
    assert bot_token not in caplog.text
    assert "[REDACTED]" in caplog.text


# --- get_notifier Factory Tests ---


def test_get_notifier_selection(tmp_path: Path):
    # 1. No secrets, no settings -> NoOpNotifier
    n1 = get_notifier(root=tmp_path)
    assert isinstance(n1, NoOpNotifier)

    # 2. Secrets present, but settings disabled -> NoOpNotifier
    save_secrets({"telegram_bot_token": "tok123"}, root=tmp_path)
    s_disabled = Settings(telegram_enabled=False, telegram_chat_id="chat123")
    n2 = get_notifier(root=tmp_path, settings=s_disabled)
    assert isinstance(n2, NoOpNotifier)

    # 3. Settings enabled, but no token in secrets -> NoOpNotifier
    empty_secrets_path = tmp_path / "empty_dir"
    empty_secrets_path.mkdir()
    s_enabled = Settings(telegram_enabled=True, telegram_chat_id="chat123")
    n3 = get_notifier(root=empty_secrets_path, settings=s_enabled)
    assert isinstance(n3, NoOpNotifier)

    # 4. Settings enabled and token present, but empty chat_id -> NoOpNotifier
    s_no_chat = Settings(telegram_enabled=True, telegram_chat_id="")
    n4 = get_notifier(root=tmp_path, settings=s_no_chat)
    assert isinstance(n4, NoOpNotifier)

    # 5. Enabled with token and chat_id -> TelegramNotifier
    s_valid = Settings(telegram_enabled=True, telegram_chat_id="chat123")
    n5 = get_notifier(root=tmp_path, settings=s_valid)
    assert isinstance(n5, TelegramNotifier)
    assert n5.bot_token == "tok123"
    assert n5.chat_id == "chat123"


# --- scripts/setup-telegram.ps1 Tests ---


def test_setup_telegram_powershell_script(tmp_path: Path):
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "setup-telegram.ps1"
    assert script_path.exists()

    bot_token = "998877:AAEEFF-test-bot-token"
    chat_id = "11223344"

    cmd = [
        "pwsh",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(script_path),
        "-BotToken",
        bot_token,
        "-ChatId",
        chat_id,
        "-DataDir",
        str(tmp_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, check=True)

    # Assert token was never printed to stdout/stderr
    assert bot_token not in result.stdout
    assert bot_token not in result.stderr
    assert "Telegram configured successfully!" in result.stdout

    # Assert secrets.json was created with bot token
    secrets_file = tmp_path / "secrets.json"
    assert secrets_file.exists()
    secrets_data = json.loads(secrets_file.read_text(encoding="utf-8"))
    assert secrets_data.get("telegram_bot_token") == bot_token

    # Assert settings.json was created with chat_id and telegram_enabled: true
    settings_file = tmp_path / "settings.json"
    assert settings_file.exists()
    settings_data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert settings_data.get("telegram_chat_id") == chat_id
    assert settings_data.get("telegram_enabled") is True

    # Assert get_notifier wiring works with the configured directory
    notifier = get_notifier(root=tmp_path)
    assert isinstance(notifier, TelegramNotifier)
    assert notifier.bot_token == bot_token
    assert notifier.chat_id == chat_id
