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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("vibedesk.notify")
BANGKOK_TZ = ZoneInfo("Asia/Bangkok")


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
        NoticeKind.CANDIDATE: "[CANDIDATE]",
        NoticeKind.PASS: "[PASS]",
        NoticeKind.HOLDOUT_FAIL: "[HOLDOUT FAIL]",
        NoticeKind.HOLDOUT_REQUEST: "[HOLDOUT REQUEST]",
        NoticeKind.TOKEN_WARNING: "[TOKEN BUDGET WARNING]",
        NoticeKind.DAILY_DIGEST: "[DAILY DIGEST]",
    }

    prefix = prefix_map.get(
        NoticeKind(kind) if kind in NoticeKind._value2member_map_ else kind  # type: ignore[arg-type]
    )
    if prefix is None:
        prefix = f"[{str(kind).upper()}]"

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
    ledger: Any = None,
    budget: Any = None,
) -> Notice:
    """Compile a DAILY_DIGEST notice from explicit counts or ledger/budget objects."""
    now_bkk = datetime.now(BANGKOK_TZ)
    if date is None:
        date = now_bkk.strftime("%Y-%m-%d")

    if budget is not None and hasattr(budget, "used"):
        tokens_used = getattr(budget, "used", tokens_used)

    if ledger is not None and hasattr(ledger, "_db") and hasattr(ledger, "_lock"):
        try:
            start_bkk = datetime(
                now_bkk.year, now_bkk.month, now_bkk.day, 0, 0, 0, tzinfo=BANGKOK_TZ
            )
            start_utc = start_bkk.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            with ledger._lock:
                row = ledger._db.execute(
                    "SELECT COUNT(*), "
                    "COALESCE(SUM(CASE WHEN verdict = 'REJECT' THEN 1 ELSE 0 END), 0) "
                    "FROM trials WHERE created_at >= ?",
                    (start_utc,),
                ).fetchone()
                if row:
                    trials_run = int(row[0])
                    reject_count = int(row[1])
        except Exception as exc:
            logger.warning("Could not query ledger for daily digest counts: %s", exc)

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
