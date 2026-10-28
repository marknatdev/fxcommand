"""Background scheduler for Layer A maintenance jobs (spec §4, §8.4, CCC-53).

Jobs:
1. `nightly_refresh`: 02:00 Asia/Bangkok (19:00 UTC) daily. Incremental import from MT5 terminal.
2. `weekly_spread_floors`: 00:00 Asia/Bangkok Sunday (Saturday 17:00 UTC). Re-measure Spread Floors.
3. `daily_digest`: 21:00 Asia/Bangkok (14:00 UTC) daily. Telegram push of REJECTs, Trials, tokens.
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
    """Manages scheduled background maintenance and reporting jobs (spec §4, §8.4, CCC-53)."""

    def __init__(
        self,
        root: Path,
        desk: Any = None,
        notifier: Any | None = None,
        budget: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        api: Any | None = None,
        symbols: Sequence[str] | None = None,
    ) -> None:
        self.root = Path(root)
        self.desk = desk
        self.notifier = notifier
        self.budget = budget
        self._clock = clock or (lambda: datetime.now(BANGKOK_TZ))
        self._api = api
        self.symbols = tuple(symbols) if symbols is not None else None
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
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
        for name, job in self._jobs.items():
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
                    except (CostError, Exception) as e:
                        logger.warning("Spread floor measurement failed for %s: %s", symbol, e)
                        errors[symbol] = str(e)
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

        # Scheduled 21:00 Bangkok time as UTC (CCC-53, coordination with Notify team)
        # Using explicit until so consecutive 24 h windows line up exactly even if late
        if scheduled_time is not None:
            scheduled_bkk = (
                scheduled_time.astimezone(BANGKOK_TZ)
                if scheduled_time.tzinfo is not None
                else scheduled_time.replace(tzinfo=BANGKOK_TZ)
            )
        else:
            scheduled_bkk = now_bkk.replace(hour=21, minute=0, second=0, microsecond=0)

        until_utc = scheduled_bkk.astimezone(UTC)

        try:
            from vibedesk.notify import compile_daily_digest
        except ImportError:
            compile_daily_digest = None

        ledger = getattr(self.desk, "ledger", None) if self.desk is not None else None
        if compile_daily_digest is not None:
            notice = compile_daily_digest(
                ledger=ledger,
                budget=self.budget,
                until=until_utc,
                now=self._clock,
            )
        else:
            # Count REJECTs and Trials from the Ledger over window (until - 24h, until]
            from vibedesk.notify import Notice, NoticeKind

            since_utc = until_utc - timedelta(hours=24)
            trials_count = 0
            rejects = 0
            if ledger is not None:
                if hasattr(ledger, "trial_counts"):
                    trials_count, rejects = ledger.trial_counts(since_utc, until_utc)
                else:
                    since_iso = since_utc.isoformat()
                    until_iso = until_utc.isoformat()
                    with getattr(ledger, "_lock", threading.Lock()):
                        db = getattr(ledger, "_db", None)
                        if db is not None:
                            row = db.execute(
                                "SELECT COUNT(*), "
                                "COALESCE(SUM(CASE WHEN verdict = 'REJECT' THEN 1 ELSE 0 END), 0) "
                                "FROM trials "
                                "WHERE datetime(created_at) > datetime(?) "
                                "AND datetime(created_at) <= datetime(?)",
                                (since_iso, until_iso),
                            ).fetchone()
                            if row:
                                trials_count, rejects = int(row[0]), int(row[1])

            tokens_used = self.budget.used if self.budget else 0
            text = (
                f"Daily Digest: {rejects} REJECTs, {trials_count} Trials run, "
                f"{tokens_used:,} tokens used today."
            )
            notice = Notice(
                kind=NoticeKind.DAILY_DIGEST,
                text=text,
                data={
                    "reject_count": rejects,
                    "trials_run": trials_count,
                    "tokens_used": tokens_used,
                    "until_utc": until_utc.isoformat(),
                },
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

    def run_job_now(self, name: str, scheduled_time: datetime | None = None) -> dict[str, Any]:
        """Manually trigger a job immediately (for testing or admin per CCC-53)."""
        if name not in self._jobs:
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
