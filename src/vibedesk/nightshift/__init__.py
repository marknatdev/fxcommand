"""Night Shift package: autonomous overnight research loop."""

from __future__ import annotations

from vibedesk.nightshift.brief import compile_morning_brief, format_morning_brief
from vibedesk.nightshift.runner import NightShiftConfig, NightShiftState, run_night_shift

__all__ = [
    "NightShiftConfig",
    "NightShiftState",
    "compile_morning_brief",
    "format_morning_brief",
    "run_night_shift",
]
