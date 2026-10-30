"""Night Shift autonomous overnight research runner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NightShiftConfig:
    """Configuration limits for Night Shift (ADR 0011)."""

    max_trials: int = 20
    token_share: float = 0.50
    morning_brief_time: str = "07:00"


@dataclass
class NightShiftState:
    """Execution status and summary for Night Shift."""

    trials_run: int = 0
    candidates_found: int = 0
    tokens_used: int = 0
    stopped_reason: str = "pending"
    started_at: str = ""
    finished_at: str = ""


def run_night_shift(
    desk: Any,
    budget: Any,
    config: NightShiftConfig | None = None,
) -> NightShiftState:
    """Run autonomous overnight loop exploring queued ideas and family variations.

    Limits: capped at config.max_trials (20) and config.token_share * daily_cap.
    May run Trials only (never spends Holdout; never sends to Arena).
    Teams implement full execution loop in Wave 1 (CCC-75).
    """
    raise NotImplementedError("run_night_shift execution loop owned by Wave 1 (CCC-75)")
