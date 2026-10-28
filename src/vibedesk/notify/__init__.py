"""Notification subsystem (spec §3.2, §3.3, §8.4): push-only notices and daily digests.

Spec §8.4:
- Sends CANDIDATE, PASS and HOLDOUT FAIL Verdicts, Holdout Requests awaiting the owner,
  and the token-budget warning.
- Sends a daily digest at 21:00 Bangkok time with REJECT count, Trials run and tokens used.
- Push-only.
- Bot token stored in data/secrets.json (git-ignored), never logged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


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


class TelegramNotifier:
    """Push-only Telegram notifier via Bot API sendMessage (spec §8.4).

    Bot token must come from data/secrets.json (or env VIBEDESK_TELEGRAM_BOT_TOKEN)
    and must never be written to logs. Stubbed for Wave 1 Notify team (CCC-59).
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        base_url: str = "https://api.telegram.org",
    ) -> None:
        if not bot_token or not chat_id:
            raise ValueError("TelegramNotifier requires bot_token and chat_id")
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = base_url.rstrip("/")

    def send(self, notice: Notice) -> bool:
        """Deliver notice via Telegram Bot API. Implemented in CCC-59."""
        raise NotImplementedError("TelegramNotifier.send will be implemented in CCC-59")


def get_notifier(root: Path | None = None, settings: Any = None) -> Notifier:
    """Wiring point for notifiers: returns TelegramNotifier if enabled and token present,
    otherwise NoOpNotifier.
    """
    from vibedesk.app.settings import DATA, load_secrets

    data_dir = root or DATA
    secrets = load_secrets(data_dir)
    token = secrets.get("telegram_bot_token", "")
    chat_id = getattr(settings, "telegram_chat_id", "") if settings else ""
    enabled = getattr(settings, "telegram_enabled", False) if settings else False

    if enabled and token and chat_id:
        return TelegramNotifier(bot_token=token, chat_id=chat_id)
    return NoOpNotifier()


__all__ = [
    "NoOpNotifier",
    "Notice",
    "NoticeKind",
    "Notifier",
    "RecordingNotifier",
    "TelegramNotifier",
    "get_notifier",
]
