"""Tests for Telegram notices, daily digest, and notifiers (spec §8.4, CCC-59, CCC-60)."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import pytest

from vibedesk.app.settings import Settings, save_secrets
from vibedesk.blocks.spec import expand_grid, validate
from vibedesk.desk.budget import TokenBudget
from vibedesk.engine.costs import CostSnapshot
from vibedesk.ledger.store import Ledger
from vibedesk.market.bars import Bars, Timeframe
from vibedesk.notify import (
    BANGKOK_TZ,
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


def test_compile_daily_digest_window_derives_date():
    # Injected now at 21:00 Bangkok on 2026-10-28
    fixed_now = datetime(2026, 10, 28, 21, 0, 0, tzinfo=BANGKOK_TZ)
    notice = compile_daily_digest(
        reject_count=3,
        trials_run=5,
        tokens_used=10000,
        now=fixed_now,
    )
    assert notice.data["date"] == "2026-10-28"
    assert notice.data["until_utc"] == fixed_now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    since_exp = (fixed_now - timedelta(hours=24)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert notice.data["since_utc"] == since_exp


def test_compile_daily_digest_naive_since_until_treated_as_utc():
    # Naive UTC ISO strings without Z are treated as UTC to match Ledger timestamps
    notice_str = compile_daily_digest(
        reject_count=2,
        trials_run=4,
        tokens_used=5000,
        since="2026-10-27T14:00:00",
        until="2026-10-28T14:00:00",
    )
    assert notice_str.data["since_utc"] == "2026-10-27T14:00:00Z"
    assert notice_str.data["until_utc"] == "2026-10-28T14:00:00Z"
    # 14:00 UTC is 21:00 Bangkok on 2026-10-28
    assert notice_str.data["date"] == "2026-10-28"

    # Naive datetime objects are also treated as UTC
    dt_until = datetime(2026, 10, 28, 14, 0, 0)
    dt_since = datetime(2026, 10, 27, 14, 0, 0)
    notice_dt = compile_daily_digest(
        reject_count=2,
        trials_run=4,
        tokens_used=5000,
        since=dt_since,
        until=dt_until,
    )
    assert notice_dt.data["since_utc"] == "2026-10-27T14:00:00Z"
    assert notice_dt.data["until_utc"] == "2026-10-28T14:00:00Z"


def test_compile_daily_digest_with_budget_and_mock_ledger():
    budget = TokenBudget(daily_cap=100000)
    budget.record_usage(42000)

    # Caller passes counts directly along with budget
    notice = compile_daily_digest(budget=budget, reject_count=10, trials_run=12, date="2026-10-28")
    assert notice.data["tokens_used"] == 42000
    assert notice.data["trials_run"] == 12
    assert notice.data["reject_count"] == 10

    # If ledger implements public trial_counts, it is used
    class MockLedger:
        def trial_counts(
            self, since_utc: str | None = None, until_utc: str | None = None
        ) -> tuple[int, int]:
            return (8, 6)

    notice_mock = compile_daily_digest(budget=budget, ledger=MockLedger(), date="2026-10-28")
    assert notice_mock.data["trials_run"] == 8
    assert notice_mock.data["reject_count"] == 6
    assert notice_mock.data["tokens_used"] == 42000

    # If an invalid ledger is passed without trial_counts, it raises AttributeError
    class InvalidLedger:
        pass

    with pytest.raises(AttributeError, match="Ledger does not implement public method"):
        compile_daily_digest(ledger=InvalidLedger())

    # If trial_counts raises, the error propagates and is not silenced
    class FailingLedger:
        def trial_counts(
            self, since_utc: str | None = None, until_utc: str | None = None
        ) -> tuple[int, int]:
            raise RuntimeError("Database connection dropped")

    with pytest.raises(RuntimeError, match="Database connection dropped"):
        compile_daily_digest(ledger=FailingLedger())


def test_compile_daily_digest_with_real_ledger(tmp_path: Path):
    ledger_path = tmp_path / "desk.db"
    with Ledger(ledger_path) as ledger:
        family = ledger.create_family("Trend following on gold")
        spec = {
            "name": "donchian_breakout",
            "symbol": "GOLD",
            "timeframe": "M15",
            "direction": "both",
            "entry": {"block": "donchian_breakout", "params": {"period": 20}},
            "exit": {
                "stop": {"block": "atr_stop", "params": {"period": 14, "mult": 2.0}},
                "target": {"block": "r_target", "params": {"r": 2.0}},
            },
            "sizing": {"block": "fixed_risk", "params": {}},
        }
        hyp = ledger.register_hypothesis(family, spec)

        time_arr = 1_420_070_400 + 900 * np.arange(8)
        close = 1200.0 + np.arange(8)
        bars = Bars(
            "GOLD",
            Timeframe.M15,
            0.01,
            2,
            time_arr,
            close,
            close + 1,
            close - 1,
            close,
            np.full(8, 30),
        )
        cost = CostSnapshot(
            0.01, 55, 1, swap_long_points=-83.83, swap_short_points=20.64, as_of="2026-10-21"
        )
        miss = {"check": "profit_factor", "value": 0.95, "threshold": 1.2}
        grid = expand_grid(validate(spec))

        # Record Trial 1: CANDIDATE
        ledger.record_trial(
            hyp,
            combinations=[(grid[0], 1.2)],
            verdict="CANDIDATE",
            closest_miss=None,
            cost=cost,
            bars=bars,
            rules_version=1,
            result_json='{"folds": [], "checks": []}',
        )

        # Record Trial 2: REJECT
        ledger.record_trial(
            hyp,
            combinations=[(grid[0], 0.8)],
            verdict="REJECT",
            closest_miss=miss,
            cost=cost,
            bars=bars,
            rules_version=1,
            result_json='{"folds": [], "checks": []}',
        )

        # Record Trial 3: REJECT
        ledger.record_trial(
            hyp,
            combinations=[(grid[0], 0.7)],
            verdict="REJECT",
            closest_miss=miss,
            cost=cost,
            bars=bars,
            rules_version=1,
            result_json='{"folds": [], "checks": []}',
        )

        budget = TokenBudget(daily_cap=100000)
        budget.record_usage(35000)

        # Digest evaluated at the current time: includes all 3 recorded trials
        now_current = datetime.now(UTC)
        notice = compile_daily_digest(ledger=ledger, budget=budget, now=now_current)
        assert notice.data["trials_run"] == 3
        assert notice.data["reject_count"] == 2
        assert notice.data["tokens_used"] == 35000
        assert "Trials run: 3" in notice.text
        assert "REJECT count: 2" in notice.text
        assert "Tokens used: 35000" in notice.text

        # Digest evaluated 26 hours in the future: window is [now + 2h, now + 26h]
        # The 3 trials recorded at `now_current` fall before the window, so they are excluded
        future_now = now_current + timedelta(hours=26)
        notice_future = compile_daily_digest(ledger=ledger, budget=budget, now=future_now)
        assert notice_future.data["trials_run"] == 0
        assert notice_future.data["reject_count"] == 0
        assert notice_future.data["tokens_used"] == 35000


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


ps_exe = shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.skipif(ps_exe is None, reason="No PowerShell executable found")
def test_setup_telegram_powershell_script(tmp_path: Path):
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "setup-telegram.ps1"
    assert script_path.exists()

    bot_token = "998877:AAEEFF-test-bot-token"
    chat_id = "11223344"

    cmd = [
        ps_exe,
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
