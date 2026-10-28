"""Background scheduler for Layer A maintenance jobs (spec §4, §8.4, CCC-53).

Jobs:
1. `nightly_refresh`: 02:00 Asia/Bangkok (19:00 UTC) daily. Incremental import from MT5 terminal.
2. `weekly_spread_floors`: 00:00 Asia/Bangkok Sunday (Saturday 17:00 UTC). Re-measure Spread Floors.
3. `daily_digest`: 21:00 Asia/Bangkok (14:00 UTC) daily. Telegram push of REJECTs, Trials, tokens.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger("vibedesk.scheduler")
BANGKOK_TZ = ZoneInfo("Asia/Bangkok")


@dataclass
class JobInfo:
    """Metadata and execution state of a scheduled job."""

    name: str
    schedule_desc: str
    description: str
    last_run: datetime | None = None
    next_run: datetime | None = None
    status: str = "idle"


class Scheduler:
    """Manages scheduled background maintenance and reporting jobs (spec §4, §8.4, CCC-53)."""

    def __init__(
        self,
        root: Path,
        desk: Any = None,
        notifier: Any | None = None,
    ) -> None:
        self.root = Path(root)
        self.desk = desk
        self.notifier = notifier
        self._running = False
        self._jobs = {
            "nightly_refresh": JobInfo(
                name="nightly_refresh",
                schedule_desc="02:00 Asia/Bangkok daily",
                description="Incremental data import of all 8 symbols from MT5 (read-only).",
            ),
            "weekly_spread_floors": JobInfo(
                name="weekly_spread_floors",
                schedule_desc="00:00 Asia/Bangkok Sunday",
                description="Re-measure Spread Floors (14-day tick p50) and swaps across symbols.",
            ),
            "daily_digest": JobInfo(
                name="daily_digest",
                schedule_desc="21:00 Asia/Bangkok daily",
                description="Compile daily digest (REJECTs, Trials, tokens) and push to Telegram.",
            ),
        }

    def start(self) -> None:
        """Start the background scheduler thread/loop. Implemented in CCC-53."""
        self._running = True
        logger.info("Scheduler started with %d jobs", len(self._jobs))

    def stop(self) -> None:
        """Stop the background scheduler."""
        self._running = False
        logger.info("Scheduler stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    def jobs(self) -> list[JobInfo]:
        """Return the list of configured jobs."""
        return list(self._jobs.values())

    def job(self, name: str) -> JobInfo | None:
        return self._jobs.get(name)

    def run_job_now(self, name: str) -> dict[str, Any]:
        """Manually trigger a job immediately (for testing or admin). Implemented in CCC-53."""
        if name not in self._jobs:
            raise KeyError(f"unknown job {name!r}; expected one of {list(self._jobs.keys())}")
        raise NotImplementedError(f"run_job_now for {name!r} will be implemented in CCC-53")


__all__ = [
    "BANGKOK_TZ",
    "JobInfo",
    "Scheduler",
]
