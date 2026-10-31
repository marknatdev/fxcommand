"""Background scheduler for Layer A maintenance jobs (spec §4, §8.4, CCC-53) and Layer B (CCC-66).

Jobs:
1. `nightly_refresh`: 02:00 Asia/Bangkok (19:00 UTC) daily. Incremental import from MT5 terminal.
2. `weekly_spread_floors`: 00:00 Asia/Bangkok Sunday (Saturday 17:00 UTC). Re-measure Spread Floors.
3. `daily_digest`: 21:00 Asia/Bangkok (14:00 UTC) daily. Telegram push of REJECTs, Trials, tokens.
4. `arena_closed_bar_poll`: Every 10 seconds. Poll MT5 for closed bars, step Arena (CCC-66).
5. `night_shift_brief`: 07:00 Asia/Bangkok daily. Morning brief to Telegram (CCC-75).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    """Manages background maintenance and reporting jobs (spec §4, §8.4, CCC-53, CCC-66)."""

    def __init__(
        self,
        root: Path,
        desk: Any = None,
        notifier: Any | None = None,
        budget: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        api: Any | None = None,
        symbols: Sequence[str] | None = None,
        arena_runtime: Any | None = None,
        enable_arena: bool | None = None,
    ) -> None:
        self.root = Path(root)
        self.desk = desk
        self.notifier = notifier
        self.budget = budget
        self._clock = clock or (lambda: datetime.now(BANGKOK_TZ))
        self._api = api
        self.symbols = tuple(symbols) if symbols is not None else None
        self.arena_runtime = arena_runtime or getattr(desk, "arena_runtime", None)
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._jobs: dict[str, JobInfo] = {
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
        if self.arena_runtime is not None or enable_arena is True:
            self.register_arena_jobs(self.arena_runtime)

    def register_arena_jobs(self, arena_runtime: Any | None = None) -> None:
        """Register Forward Arena poll and Night Shift brief (spec §2.1, §6.2, CCC-66)."""
        if arena_runtime is not None:
            self.arena_runtime = arena_runtime
        now = self._clock()
        if "arena_closed_bar_poll" not in self._jobs:
            job = JobInfo(
                name="arena_closed_bar_poll",
                schedule_desc="Every 10 seconds",
                description=(
                    "Poll MT5 for newly closed bars, append to cache, step Arena strategies."
                ),
            )
            job.next_run = self._compute_next_run("arena_closed_bar_poll", now)
            self._jobs["arena_closed_bar_poll"] = job

        if "night_shift_brief" not in self._jobs:
            job = JobInfo(
                name="night_shift_brief",
                schedule_desc="07:00 Asia/Bangkok daily",
                description=(
                    "Compile and dispatch Night Shift morning brief to Telegram (spec §6.2)."
                ),
            )
            job.next_run = self._compute_next_run("night_shift_brief", now)
            self._jobs["night_shift_brief"] = job

    def _compute_next_run(self, name: str, now: datetime) -> datetime:
        """Compute the next scheduled run time in Asia/Bangkok."""
        current = now if now.tzinfo is not None else now.replace(tzinfo=BANGKOK_TZ)
        current = current.astimezone(BANGKOK_TZ)

        if name == "nightly_refresh":
            target = current.replace(hour=2, minute=0, second=0, microsecond=0)
            return target if current < target else target + timedelta(days=1)
        elif name == "daily_digest":
            target = current.replace(hour=21, minute=0, second=0, microsecond=0)
            return target if current < target else target + timedelta(days=1)
        elif name == "weekly_spread_floors":
            # Sunday is weekday 6
            days_ahead = (6 - current.weekday()) % 7
            target = (current + timedelta(days=days_ahead)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            return target if current < target else target + timedelta(days=7)
        elif name == "night_shift_brief":
            target = current.replace(hour=7, minute=0, second=0, microsecond=0)
            return target if current < target else target + timedelta(days=1)
        elif name == "arena_closed_bar_poll":
            return current + timedelta(seconds=10)
        raise KeyError(f"unknown job {name!r}")

    def start(self) -> None:
        """Start the background scheduler thread/loop (spec §4, CCC-53)."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        now = self._clock()
        for name, job in self._jobs.items():
            if job.next_run is None:
                job.next_run = self._compute_next_run(name, now)
        self._thread = threading.Thread(target=self._worker, daemon=True, name="vibedesk-scheduler")
        self._thread.start()
        logger.info("Scheduler started with %d jobs", len(self._jobs))

    def stop(self) -> None:
        """Stop the background scheduler."""
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        logger.info("Scheduler stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    def jobs(self) -> list[JobInfo]:
        """Return the list of configured jobs."""
        return list(self._jobs.values())

    def job(self, name: str) -> JobInfo | None:
        return self._jobs.get(name)

    def tick(self, now: datetime | None = None) -> list[str]:
        """Check scheduled jobs and execute any whose time has arrived (CCC-53)."""
        current_time = now or self._clock()
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=BANGKOK_TZ)
        else:
            current_time = current_time.astimezone(BANGKOK_TZ)

        ran: list[str] = []
        for name, job in list(self._jobs.items()):
            if job.next_run is None:
                job.next_run = self._compute_next_run(name, current_time)
            elif current_time >= job.next_run:
                scheduled_time = job.next_run
                self.run_job_now(name, scheduled_time=scheduled_time)
                job.next_run = self._compute_next_run(name, current_time)
                ran.append(name)
        return ran

    def _worker(self) -> None:
        while not self._stop_event.wait(timeout=1.0):
            if not self._running:
                break
            try:
                self.tick()
            except Exception as e:
                logger.error("Error in scheduler tick: %s", e)

    def _run_nightly_refresh(self) -> dict[str, Any]:
        from vibedesk.app.desk import MT5_LOCK
        from vibedesk.market.cache import CacheError, read_meta
        from vibedesk.market.importer import import_history, refresh
        from vibedesk.market.universe import SYMBOLS

        api = self._api
        if api is None:
            try:
                from vibedesk.market.mt5_api import real_api

                api = real_api()
            except Exception as e:
                return {
                    "job": "nightly_refresh",
                    "status": "error",
                    "error": f"MT5 api unavailable: {e}",
                }

        imported = 0
        errors: dict[str, str] = {}
        target_symbols = self.symbols or SYMBOLS
        with MT5_LOCK:
            for symbol in target_symbols:
                try:
                    try:
                        known = read_meta(self.root, symbol).last_import is not None
                    except CacheError:
                        known = False
                    run = refresh if known else import_history
                    run(api, self.root, symbols=(symbol,))
                    imported += 1
                except Exception as e:
                    logger.warning("Nightly refresh failed for %s: %s", symbol, e)
                    errors[symbol] = str(e)

        status = "ok" if not errors else ("partial" if imported > 0 else "error")
        res: dict[str, Any] = {"job": "nightly_refresh", "status": status, "symbols": imported}
        if errors:
            res["errors"] = errors
        return res

    def _run_weekly_spread_floors(self) -> dict[str, Any]:
        from vibedesk.app.desk import MT5_LOCK
        from vibedesk.market.costs import CostError, measure_cost, record_cost
        from vibedesk.market.universe import SYMBOLS

        api = self._api
        if api is None:
            try:
                from vibedesk.market.mt5_api import real_api

                api = real_api()
            except Exception as e:
                return {
                    "job": "weekly_spread_floors",
                    "status": "error",
                    "error": f"MT5 api unavailable: {e}",
                }

        clock_time = self._clock()
        if clock_time.tzinfo is not None:
            now = clock_time.astimezone(UTC)
        else:
            now = clock_time.replace(tzinfo=BANGKOK_TZ).astimezone(UTC)

        measured = 0
        errors: dict[str, str] = {}
        target_symbols = self.symbols or SYMBOLS

        with MT5_LOCK:
            if not api.initialize():
                err = api.last_error() if hasattr(api, "last_error") else "initialize failed"
                return {
                    "job": "weekly_spread_floors",
                    "status": "error",
                    "error": f"MT5 initialize failed: {err}",
                }
            try:
                for symbol in target_symbols:
                    try:
                        api.symbol_select(symbol, True)
                        cost = measure_cost(api, symbol, now)
                        record_cost(self.root, cost)
                        measured += 1
                    except CostError as e:
                        logger.warning("Spread floor measurement failed for %s: %s", symbol, e)
                        errors[symbol] = str(e)
                    except Exception as e:
                        logger.exception(
                            "Unexpected error measuring spread floor for %s: %s", symbol, e
                        )
                        errors[symbol] = f"Unexpected: {e}"
            finally:
                api.shutdown()

        status = "ok" if not errors else ("partial" if measured > 0 else "error")
        res: dict[str, Any] = {"job": "weekly_spread_floors", "status": status, "symbols": measured}
        if errors:
            res["errors"] = errors
        return res

    def _run_daily_digest(self, scheduled_time: datetime | None = None) -> dict[str, Any]:
        clock_time = self._clock()
        if clock_time.tzinfo is not None:
            now_bkk = clock_time.astimezone(BANGKOK_TZ)
        else:
            now_bkk = clock_time.replace(tzinfo=BANGKOK_TZ)

        if scheduled_time is not None:
            scheduled_bkk = (
                scheduled_time.astimezone(BANGKOK_TZ)
                if scheduled_time.tzinfo is not None
                else scheduled_time.replace(tzinfo=BANGKOK_TZ)
            )
            until_utc = scheduled_bkk.astimezone(UTC)
        else:
            until_utc = now_bkk.astimezone(UTC)

        from vibedesk.notify import compile_daily_digest

        ledger = getattr(self.desk, "ledger", None) if self.desk is not None else None
        notice = compile_daily_digest(
            ledger=ledger,
            budget=self.budget,
            until=until_utc,
            now=self._clock,
        )

        sent = False
        if self.notifier is not None:
            sent = self.notifier.send(notice)

        rejects = notice.data.get("reject_count", 0)
        trials_count = notice.data.get("trials_run", 0)
        tokens_used = notice.data.get("tokens_used", 0)

        return {
            "job": "daily_digest",
            "status": "ok",
            "rejects": rejects,
            "trials": trials_count,
            "tokens_used": tokens_used,
            "sent": sent,
            "until_utc": until_utc.isoformat(),
        }

    def _run_arena_closed_bar_poll(self) -> dict[str, Any]:
        """Poll MT5 for newly closed bars, append to cache, and step Arena strategies (CCC-66)."""
        if self.arena_runtime is None:
            return {"job": "arena_closed_bar_poll", "status": "ok", "stepped": 0}
        try:
            results = self.arena_runtime.poll_closed_bars(
                root=self.root,
                api=self._api,
                clock=self._clock,
            )
            return {
                "job": "arena_closed_bar_poll",
                "status": "ok",
                "stepped": len(results),
                "results": results,
            }
        except Exception as e:
            logger.exception("Error in arena closed bar poll: %s", e)
            return {"job": "arena_closed_bar_poll", "status": "error", "error": str(e)}

    def _run_night_shift_brief(self, scheduled_time: datetime | None = None) -> dict[str, Any]:
        """Compile and send 07:00 Bangkok Night Shift morning brief (spec §6.2, CCC-75)."""
        from vibedesk.nightshift import NightShiftState, compile_morning_brief

        state = getattr(self.desk, "night_shift_state", None) or NightShiftState()
        notice = compile_morning_brief(state)
        sent = False
        if self.notifier is not None:
            sent = self.notifier.send(notice)

        return {
            "job": "night_shift_brief",
            "status": "ok",
            "sent": sent,
            "trials_run": state.trials_run,
            "candidates_found": state.candidates_found,
            "tokens_used": state.tokens_used,
            "stopped_reason": state.stopped_reason,
        }

    def run_job_now(self, name: str, scheduled_time: datetime | None = None) -> dict[str, Any]:
        """Manually trigger a job immediately (for testing or admin per CCC-53)."""
        if name not in self._jobs:
            if name in ("arena_closed_bar_poll", "night_shift_brief"):
                self.register_arena_jobs()
            else:
                raise KeyError(f"unknown job {name!r}; expected one of {list(self._jobs.keys())}")
        job = self._jobs[name]
        job.status = "running"
        now = self._clock()
        try:
            if name == "nightly_refresh":
                res = self._run_nightly_refresh()
            elif name == "weekly_spread_floors":
                res = self._run_weekly_spread_floors()
            elif name == "daily_digest":
                res = self._run_daily_digest(scheduled_time=scheduled_time)
            elif name == "arena_closed_bar_poll":
                res = self._run_arena_closed_bar_poll()
            elif name == "night_shift_brief":
                res = self._run_night_shift_brief(scheduled_time=scheduled_time)
            else:
                raise KeyError(name)
            job.last_run = now
            job.status = "idle" if res.get("status") == "ok" else "error"
            return res
        except Exception as e:
            job.last_run = now
            job.status = "error"
            return {"job": name, "status": "error", "error": str(e)}


__all__ = [
    "BANGKOK_TZ",
    "JobInfo",
    "Scheduler",
]
