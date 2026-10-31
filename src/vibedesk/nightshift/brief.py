"""Formatting and compilation for the 07:00 Bangkok Night Shift morning brief."""

from __future__ import annotations

from vibedesk.nightshift.runner import NightShiftState
from vibedesk.notify import Notice, NoticeKind


def format_morning_brief(state: NightShiftState) -> str:
    """Format Telegram text for the morning brief (spec §6.2)."""
    return (
        "Vibe Desk Night Shift Brief (07:00 Bangkok)\n"
        f"- Trials run: {state.trials_run}\n"
        f"- CANDIDATEs found: {state.candidates_found}\n"
        f"- Tokens used: {state.tokens_used}\n"
        f"- Stop reason: {state.stopped_reason}"
    )


def compile_morning_brief(state: NightShiftState) -> Notice:
    """Compile morning brief Notice for dispatch."""
    text = format_morning_brief(state)
    return Notice(
        kind=NoticeKind.DAILY_DIGEST,
        text=text,
        data={
            "trials_run": state.trials_run,
            "candidates_found": state.candidates_found,
            "tokens_used": state.tokens_used,
            "stopped_reason": state.stopped_reason,
            "brief_type": "night_shift_morning",
        },
    )
