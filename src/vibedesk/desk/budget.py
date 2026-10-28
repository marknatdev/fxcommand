"""Daily token budget (spec §7.4, CCC-46): cap from OmniRoute usage, 80% warn, pause at 100%.

Daily reset is anchored to 00:00 Asia/Bangkok time (UTC+7), aligning with the owner's
local desk operations and the 21:00 Bangkok daily digest (spec §4, §8.4).
Clock is injected via `now: Callable[[], datetime]` (defaulting to UTC wall clock)
for deterministic testing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

BANGKOK_TZ = ZoneInfo("Asia/Bangkok")
WARN_THRESHOLD = 0.80
PAUSE_THRESHOLD = 1.00


def _utc_now() -> datetime:
    return datetime.now(UTC)


class BudgetState(StrEnum):
    OK = "ok"
    WARN = "warn"
    PAUSED = "paused"


@dataclass
class TokenBudget:
    """Tracks daily LLM token consumption against the cap (spec §7.4)."""

    daily_cap: int = 100_000
    tz: ZoneInfo = BANGKOK_TZ
    now: Callable[[], datetime] = field(default=_utc_now)
    _used: int = 0
    _current_day: str = field(default="")
    _warned_today: bool = False

    def __post_init__(self) -> None:
        if not self._current_day:
            self._current_day = self._current_bangkok_date()

    def _current_bangkok_date(self) -> str:
        dt = self.now()
        if dt.tzinfo is None:
            raise ValueError("TokenBudget clock must return a timezone-aware datetime")
        return dt.astimezone(self.tz).strftime("%Y-%m-%d")

    def _check_reset(self) -> bool:
        today = self._current_bangkok_date()
        # Reset only when the Bangkok calendar date moves forward
        if today > self._current_day:
            self._current_day = today
            self._used = 0
            self._warned_today = False
            return True
        return False

    def record_usage(self, usage: dict[str, Any] | int) -> BudgetState:
        """Record token consumption from OmniRoute's usage field or an integer count."""
        self._check_reset()
        tokens = usage if isinstance(usage, int) else int(usage.get("total_tokens", 0))
        self._used += max(0, tokens)
        return self.state()

    def state(self) -> BudgetState:
        """Current state: OK (<80%), WARN (80-99%), PAUSED (>=100%)."""
        self._check_reset()
        if self.daily_cap <= 0:
            return BudgetState.OK
        ratio = self._used / self.daily_cap
        if ratio >= PAUSE_THRESHOLD:
            return BudgetState.PAUSED
        if ratio >= WARN_THRESHOLD:
            return BudgetState.WARN
        return BudgetState.OK

    @property
    def used(self) -> int:
        self._check_reset()
        return self._used

    @property
    def remaining(self) -> int:
        self._check_reset()
        if self.daily_cap <= 0:
            return 999_999_999
        return max(0, self.daily_cap - self._used)

    @property
    def is_paused(self) -> bool:
        return self.state() == BudgetState.PAUSED

    @property
    def needs_warning(self) -> bool:
        """True if crossed 80% threshold and has not yet sent a warning today."""
        self._check_reset()
        return bool(
            not self._warned_today and self.state() in (BudgetState.WARN, BudgetState.PAUSED)
        )

    def mark_warned(self) -> None:
        self._warned_today = True

    def status(self) -> dict[str, Any]:
        """Status dictionary for Settings and Health APIs."""
        st = self.state()
        return {
            "daily_cap": self.daily_cap,
            "tokens_used": self._used,
            "tokens_remaining": self.remaining,
            "state": st.value,
            "paused": st == BudgetState.PAUSED,
            "day": self._current_day,
            "timezone": str(self.tz),
        }


__all__ = [
    "BANGKOK_TZ",
    "PAUSE_THRESHOLD",
    "WARN_THRESHOLD",
    "BudgetState",
    "TokenBudget",
]
