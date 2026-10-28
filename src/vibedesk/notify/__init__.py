"""Notification subsystem (spec §3.2, §3.3, §8.4): push-only notices and daily digests.

Spec §8.4:
- Sends CANDIDATE, PASS and HOLDOUT FAIL Verdicts, Holdout Requests awaiting the owner,
  and the token-budget warning.
- Sends a daily digest at 21:00 Bangkok time with REJECT count, Trials run and tokens used.
- Push-only.
- Bot token stored in data/secrets.json (git-ignored), never logged or included in errors.

Settings Precedence Notes:
- telegram_enabled uses an OR rule (the UI cannot switch Telegram off while
  VIBEDESK_TELEGRAM_ENABLED=1).
- A non-empty settings.json string cannot be cleared back to the env value through the form.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("vibedesk.notify")
BANGKOK_TZ = ZoneInfo("Asia/Bangkok")

# Suppress httpx request logging at INFO level, as its log lines contain full URLs
# with the Telegram bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)


class NoticeKind(StrEnum):
    """Notice types emitted across Layer A (spec §8.4)."""

    CANDIDATE = "candidate"
    PASS = "pass"
    HOLDOUT_FAIL = "holdout_fail"
    HOLDOUT_REQUEST = "holdout_request"
    TOKEN_WARNING = "token_warning"
    DAILY_DIGEST = "daily_digest"


@dataclass(frozen=True)
class Notice:
    """One push notification payload."""

    kind: str  # One of NoticeKind
    text: str
    data: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class Notifier(Protocol):
    """Notifier protocol (spec §3.3)."""

    def send(self, notice: Notice) -> bool:
        """Push a notice. Return True if delivered or recorded, False otherwise."""
        ...


class RecordingNotifier:
    """In-memory notifier for tests and deterministic fakes (spec §3.3). Fully implemented."""

    def __init__(self) -> None:
        self.notices: list[Notice] = []

    def send(self, notice: Notice) -> bool:
        self.notices.append(notice)
        return True

    def clear(self) -> None:
        self.notices.clear()

    def by_kind(self, kind: str | NoticeKind) -> list[Notice]:
        target = kind.value if isinstance(kind, NoticeKind) else kind
        return [n for n in self.notices if n.kind == target]

    @property
    def last(self) -> Notice | None:
        return self.notices[-1] if self.notices else None

    def __len__(self) -> int:
        return len(self.notices)


class NoOpNotifier:
    """Null notifier for production runs when notifications are disabled or unconfigured."""

    def send(self, notice: Notice) -> bool:
        return True


def format_notice(notice: Notice) -> str:
    """Format a notice into display text for Telegram push delivery."""
    kind = notice.kind
    text = notice.text.strip() if notice.text else ""

    prefix_map = {
        "candidate": "[CANDIDATE]",
        "pass": "[PASS]",
        "holdout_fail": "[HOLDOUT FAIL]",
        "holdout_request": "[HOLDOUT REQUEST]",
        "token_warning": "[TOKEN BUDGET WARNING]",
        "daily_digest": "[DAILY DIGEST]",
    }

    kind_val = kind.value if isinstance(kind, NoticeKind) else str(kind).lower()
    prefix = prefix_map.get(kind_val, f"[{str(kind).upper()}]")

    # If text already starts with the prefix or bracketed kind, don't duplicate
    if text.upper().startswith(prefix.upper()):
        return text
    if text.upper().startswith(f"[{str(kind).upper()}]"):
        return text

    if text:
        return f"{prefix} {text}"
    return prefix


def format_daily_digest(
    *,
    reject_count: int,
    trials_run: int,
    tokens_used: int,
    date: str | None = None,
) -> str:
    """Format daily digest text containing REJECT count, Trials run, and tokens used (spec §8.4)."""
    header = f"Vibe Desk Daily Digest ({date})" if date else "Vibe Desk Daily Digest"
    return (
        f"{header}\n"
        f"- Trials run: {trials_run}\n"
        f"- REJECT count: {reject_count}\n"
        f"- Tokens used: {tokens_used}"
    )


def compile_daily_digest(
    *,
    reject_count: int = 0,
    trials_run: int = 0,
    tokens_used: int = 0,
    date: str | None = None,
    since: str | datetime | None = None,
    until: str | datetime | None = None,
    now: Callable[[], datetime] | datetime | None = None,
    ledger: Any = None,
    budget: Any = None,
) -> Notice:
    """Compile a DAILY_DIGEST notice (spec §8.4).

    Windowing:
    - By default, uses a 24-hour rolling window ending at `now` (or current Bangkok time),
      ensuring trials recorded in the 21:00-24:00 period on day N are counted in the next
      digest at 21:00 on day N+1.
    - `since` and `until` can be explicitly provided as datetimes or UTC ISO strings.
    - The date label is derived from the window's `until` timestamp (in Bangkok time)
      unless explicitly overridden via `date`.

    Data sources:
    - `ledger`: if provided, queries `ledger.trial_counts(since_utc, until_utc)`.
      Real query failures raise directly rather than reporting misleading zeros.
    - `budget`: if provided, reads `budget.used`. Note: tokens_used reflects the TokenBudget
      consumption, which resets daily at 00:00 Asia/Bangkok.
    - `reject_count`, `trials_run`, `tokens_used`: if ledger/budget are omitted, uses these
      explicit counts.
    """
    if callable(now):
        current_now = now()
    elif isinstance(now, datetime):
        current_now = now
    else:
        current_now = datetime.now(BANGKOK_TZ)

    if current_now.tzinfo is None:
        current_now = current_now.replace(tzinfo=BANGKOK_TZ)

    # Resolve `until`
    if until is None:
        until_dt = current_now
    elif isinstance(until, datetime):
        until_dt = until if until.tzinfo is not None else until.replace(tzinfo=BANGKOK_TZ)
    else:
        until_dt = datetime.fromisoformat(until)
        if until_dt.tzinfo is None:
            until_dt = until_dt.replace(tzinfo=BANGKOK_TZ)

    # Resolve `since`
    if since is None:
        since_dt = until_dt - timedelta(hours=24)
    elif isinstance(since, datetime):
        since_dt = since if since.tzinfo is not None else since.replace(tzinfo=BANGKOK_TZ)
    else:
        since_dt = datetime.fromisoformat(since)
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=BANGKOK_TZ)

    since_utc = since_dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    until_utc = until_dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    if date is None:
        date = until_dt.astimezone(BANGKOK_TZ).strftime("%Y-%m-%d")

    if budget is not None:
        if hasattr(budget, "used"):
            tokens_used = budget.used
        else:
            raise AttributeError(
                "Budget does not implement 'used'; caller must pass tokens_used directly."
            )

    if ledger is not None:
        if hasattr(ledger, "trial_counts"):
            trials_run, reject_count = ledger.trial_counts(since_utc=since_utc, until_utc=until_utc)
        else:
            raise AttributeError(
                "Ledger does not implement public method 'trial_counts'; "
                "caller must pass reject_count and trials_run directly."
            )

    text = format_daily_digest(
        reject_count=reject_count,
        trials_run=trials_run,
        tokens_used=tokens_used,
        date=date,
    )
    return Notice(
        kind=NoticeKind.DAILY_DIGEST,
        text=text,
        data={
            "reject_count": reject_count,
            "trials_run": trials_run,
            "tokens_used": tokens_used,
            "date": date,
            "since_utc": since_utc,
            "until_utc": until_utc,
        },
    )


def candidate_notice(
    text: str,
    *,
    data: dict[str, Any] | None = None,
) -> Notice:
    """Helper to construct a CANDIDATE Verdict notice."""
    return Notice(kind=NoticeKind.CANDIDATE, text=text, data=data or {})


def pass_notice(
    text: str,
    *,
    data: dict[str, Any] | None = None,
) -> Notice:
    """Helper to construct a PASS Verdict notice."""
    return Notice(kind=NoticeKind.PASS, text=text, data=data or {})


def holdout_fail_notice(
    text: str,
    *,
    data: dict[str, Any] | None = None,
) -> Notice:
    """Helper to construct a HOLDOUT FAIL Verdict notice."""
    return Notice(kind=NoticeKind.HOLDOUT_FAIL, text=text, data=data or {})


def holdout_request_notice(
    text: str,
    *,
    data: dict[str, Any] | None = None,
) -> Notice:
    """Helper to construct a Holdout Request notice awaiting owner approval."""
    return Notice(kind=NoticeKind.HOLDOUT_REQUEST, text=text, data=data or {})


def token_warning_notice(
    text: str,
    *,
    data: dict[str, Any] | None = None,
) -> Notice:
    """Helper to construct a token-budget warning notice."""
    return Notice(kind=NoticeKind.TOKEN_WARNING, text=text, data=data or {})


class TelegramNotifier:
    """Push-only Telegram notifier via Bot API sendMessage (spec §8.4).

    Bot token must come from data/secrets.json (or env VIBEDESK_TELEGRAM_BOT_TOKEN)
    and must never be written to logs or error messages.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        base_url: str = "https://api.telegram.org",
        *,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not bot_token or not chat_id:
            raise ValueError("TelegramNotifier requires bot_token and chat_id")
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.base_url = base_url.rstrip("/")
        self._client = client
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"TelegramNotifier(chat_id={self.chat_id!r}, bot_token='[REDACTED]')"

    def _redact(self, msg: str) -> str:
        if self.bot_token:
            return msg.replace(self.bot_token, "[REDACTED]")
        return msg

    def send(self, notice: Notice) -> bool:
        """Deliver notice via Telegram Bot API sendMessage.

        Returns True if successfully sent, False otherwise.
        Never logs or raises with the bot token.
        """
        endpoint = f"{self.base_url}/bot{self.bot_token}/sendMessage"
        formatted_text = format_notice(notice)
        payload = {
            "chat_id": self.chat_id,
            "text": formatted_text,
        }

        try:
            if self._client is not None:
                response = self._client.post(endpoint, json=payload, timeout=self.timeout)
            else:
                response = httpx.post(endpoint, json=payload, timeout=self.timeout)

            if response.status_code == 200:
                try:
                    data = response.json()
                    if isinstance(data, dict) and data.get("ok") is True:
                        logger.info("Delivered notice %s to Telegram", notice.kind)
                        return True
                    desc = (
                        data.get("description", "Unknown error")
                        if isinstance(data, dict)
                        else str(data)
                    )
                    logger.warning("Telegram API rejected message: %s", self._redact(str(desc)))
                    return False
                except Exception as parse_err:
                    logger.warning(
                        "Failed to parse Telegram response: %s",
                        self._redact(str(parse_err)),
                    )
                    return False
            else:
                logger.warning(
                    "Telegram HTTP status %d: %s",
                    response.status_code,
                    self._redact(response.text),
                )
                return False
        except Exception as exc:
            logger.warning("Failed to deliver Telegram notice: %s", self._redact(str(exc)))
            return False


def get_notifier(
    root: Path | None = None,
    settings: Any = None,
    client: httpx.Client | None = None,
) -> Notifier:
    """Wiring point for notifiers: returns TelegramNotifier if enabled and token present,
    otherwise NoOpNotifier.
    """
    from vibedesk.app.settings import DATA, load, load_secrets

    data_dir = root or DATA
    secrets = load_secrets(data_dir)
    token = secrets.get("telegram_bot_token", "")

    if settings is None:
        try:
            settings = load(root=data_dir)
        except Exception:
            settings = None

    chat_id = getattr(settings, "telegram_chat_id", "") if settings else ""
    enabled = getattr(settings, "telegram_enabled", False) if settings else False

    if enabled and token and chat_id:
        return TelegramNotifier(bot_token=token, chat_id=chat_id, client=client)
    return NoOpNotifier()


__all__ = [
    "BANGKOK_TZ",
    "NoOpNotifier",
    "Notice",
    "NoticeKind",
    "Notifier",
    "RecordingNotifier",
    "TelegramNotifier",
    "candidate_notice",
    "compile_daily_digest",
    "format_daily_digest",
    "format_notice",
    "get_notifier",
    "holdout_fail_notice",
    "holdout_request_notice",
    "pass_notice",
    "token_warning_notice",
]
