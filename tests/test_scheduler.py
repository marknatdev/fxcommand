"""Tests for background scheduler (spec §4, §8.4, CCC-53).

Tests cover:
- Lifecycle: start, stop, is_running
- Configured maintenance jobs: nightly_refresh, weekly_spread_floors, daily_digest
- Immediate manual/test execution via run_job_now
- Testable with an injected clock and FakeMt5 without touching real MT5
- Notification push on daily digest
- Timing triggers on Bangkok schedule (02:00, 21:00, Sunday 00:00)
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from vibedesk.app.desk import Desk
from vibedesk.app.research import EXAMPLE
from vibedesk.app.scheduler import BANGKOK_TZ, Scheduler
from vibedesk.desk.budget import TokenBudget
from vibedesk.market.costs import current_cost
from vibedesk.market.fake_mt5 import FakeMt5
from vibedesk.market.synthetic import SyntheticBarSource
from vibedesk.notify import Notice, NoticeKind, RecordingNotifier


def make_fake_mt5(now: datetime | None = None) -> FakeMt5:
    now_dt = now or datetime(2026, 10, 24, 0, 0, tzinfo=UTC)
    source = SyntheticBarSource(1, date(2013, 12, 1), date(2027, 1, 1))
    return FakeMt5(source, now=now_dt)


def test_scheduler_lifecycle(tmp_path: Path):
    scheduler = Scheduler(root=tmp_path)
    assert not scheduler.is_running
    assert len(scheduler.jobs()) == 3

    scheduler.start()
    assert scheduler.is_running
    # Idempotent start
    scheduler.start()
    assert scheduler.is_running

    scheduler.stop()
    assert not scheduler.is_running
    # Idempotent stop
    scheduler.stop()
    assert not scheduler.is_running


def test_scheduler_job_metadata(tmp_path: Path):
    scheduler = Scheduler(root=tmp_path)
    job_names = {j.name for j in scheduler.jobs()}
    assert job_names == {"nightly_refresh", "weekly_spread_floors", "daily_digest"}

    refresh_job = scheduler.job("nightly_refresh")
    assert refresh_job is not None
    assert "02:00 Asia/Bangkok" in refresh_job.schedule_desc

    floors_job = scheduler.job("weekly_spread_floors")
    assert floors_job is not None
    assert "Sunday" in floors_job.schedule_desc

    digest_job = scheduler.job("daily_digest")
    assert digest_job is not None
    assert "21:00 Asia/Bangkok" in digest_job.schedule_desc

    assert scheduler.job("unknown") is None


def test_run_job_now_nightly_refresh_with_fake_mt5(tmp_path: Path):
    fake_api = make_fake_mt5()
    scheduler = Scheduler(root=tmp_path, api=fake_api, symbols=("GOLD",))

    res = scheduler.run_job_now("nightly_refresh")
    assert res["job"] == "nightly_refresh"
    assert res["status"] == "ok"
    assert res["symbols"] == 1

    job = scheduler.job("nightly_refresh")
    assert job is not None
    assert job.last_run is not None
    assert job.status == "idle"


def test_run_job_now_weekly_spread_floors_with_fake_mt5(tmp_path: Path):
    now = datetime(2026, 10, 24, 0, 0, tzinfo=UTC)
    fake_api = make_fake_mt5(now)
    scheduler = Scheduler(
        root=tmp_path,
        api=fake_api,
        symbols=("GOLD",),
        clock=lambda: now,
    )

    res = scheduler.run_job_now("weekly_spread_floors")
    assert res["job"] == "weekly_spread_floors"
    assert res["status"] == "ok"
    assert res["symbols"] == 1

    # Verify a spread floor was recorded
    cost = current_cost(tmp_path, "GOLD")
    assert cost.spread_floor_points > 0


def test_run_job_now_daily_digest_pushes_notice(tmp_path: Path):
    notifier = RecordingNotifier()
    budget = TokenBudget(daily_cap=100_000)
    budget.record_usage(45_000)
    desk = Desk(tmp_path)
    # create a family and some trials
    fam_id = desk.ledger.create_family("Digest Test Family")
    assert fam_id > 0

    scheduler = Scheduler(
        root=tmp_path,
        desk=desk,
        notifier=notifier,
        budget=budget,
    )

    res = scheduler.run_job_now("daily_digest")
    assert res["job"] == "daily_digest"
    assert res["status"] == "ok"
    assert res["tokens_used"] == 45_000
    assert res["sent"] is True

    # Notice was recorded in notifier
    assert len(notifier) == 1
    last_notice = notifier.last
    assert last_notice is not None
    assert last_notice.kind == NoticeKind.DAILY_DIGEST
    assert last_notice.data["tokens_used"] == 45_000


def test_scheduler_timing_with_injected_clock(tmp_path: Path):
    # Virtual clock starting on Saturday at 01:59 Bangkok time
    virtual_time = datetime(2026, 10, 24, 1, 59, 0, tzinfo=BANGKOK_TZ)
    fake_api = make_fake_mt5()
    notifier = RecordingNotifier()

    scheduler = Scheduler(
        root=tmp_path,
        clock=lambda: virtual_time,
        api=fake_api,
        notifier=notifier,
        symbols=("GOLD",),
    )

    # 1. At 01:59: next_run initialized, nothing runs
    ran = scheduler.tick()
    assert ran == []
    refresh_job = scheduler.job("nightly_refresh")
    assert refresh_job is not None
    assert refresh_job.next_run == datetime(2026, 10, 24, 2, 0, 0, tzinfo=BANGKOK_TZ)

    # 2. Advance clock to 02:00: nightly_refresh triggers
    virtual_time = datetime(2026, 10, 24, 2, 0, 0, tzinfo=BANGKOK_TZ)
    ran2 = scheduler.tick()
    assert "nightly_refresh" in ran2
    # Next run advances to tomorrow 02:00
    assert refresh_job.next_run == datetime(2026, 10, 25, 2, 0, 0, tzinfo=BANGKOK_TZ)

    # 3. Advance clock to 21:00: daily_digest triggers
    virtual_time = datetime(2026, 10, 24, 21, 0, 0, tzinfo=BANGKOK_TZ)
    ran3 = scheduler.tick()
    assert "daily_digest" in ran3
    digest_job = scheduler.job("daily_digest")
    assert digest_job is not None
    assert digest_job.next_run == datetime(2026, 10, 25, 21, 0, 0, tzinfo=BANGKOK_TZ)

    # 4. Advance clock to Sunday 00:00: weekly_spread_floors triggers
    # 2026-10-25 is Sunday
    virtual_time = datetime(2026, 10, 25, 0, 0, 0, tzinfo=BANGKOK_TZ)
    ran4 = scheduler.tick()
    assert "weekly_spread_floors" in ran4
    floors_job = scheduler.job("weekly_spread_floors")
    assert floors_job is not None
    assert floors_job.next_run == datetime(2026, 11, 1, 0, 0, 0, tzinfo=BANGKOK_TZ)


def test_run_job_now_unknown_job_raises_keyerror(tmp_path: Path):
    scheduler = Scheduler(root=tmp_path)
    with pytest.raises(KeyError, match="unknown job"):
        scheduler.run_job_now("nonexistent_job")


def test_daily_digest_explicit_until_and_notify_path(tmp_path: Path):
    """Daily digest calls notify's compile_daily_digest with explicit until = 21:00 Bangkok."""
    captured_calls = []

    def mock_compile_daily_digest(*, ledger=None, budget=None, until=None, now=None, **kwargs):
        captured_calls.append({"ledger": ledger, "budget": budget, "until": until, "now": now})
        return Notice(
            kind=NoticeKind.DAILY_DIGEST,
            text="Mock digest",
            data={
                "reject_count": 3,
                "trials_run": 7,
                "tokens_used": 15000,
                "until_utc": str(until),
            },
        )

    notifier = RecordingNotifier()
    budget = TokenBudget(daily_cap=100_000)
    budget.record_usage(15_000)
    desk = Desk(tmp_path)

    # Virtual clock: fires late at 21:07:30 Bangkok time
    clock_time = datetime(2026, 10, 28, 21, 7, 30, tzinfo=BANGKOK_TZ)
    scheduled_2100 = datetime(2026, 10, 28, 21, 0, 0, tzinfo=BANGKOK_TZ)
    expected_until_utc = scheduled_2100.astimezone(UTC)

    scheduler = Scheduler(
        root=tmp_path,
        desk=desk,
        notifier=notifier,
        budget=budget,
        clock=lambda: clock_time,
    )

    import vibedesk.notify as vn

    orig_compile = getattr(vn, "compile_daily_digest", None)
    vn.compile_daily_digest = mock_compile_daily_digest
    try:
        # Tick the scheduler so daily_digest executes with scheduled_time = 21:00 Bangkok
        job = scheduler.job("daily_digest")
        assert job is not None
        job.next_run = scheduled_2100

        ran = scheduler.tick()
        assert "daily_digest" in ran

        # Verify notify was called with explicit scheduled 21:00 Bangkok (as UTC)
        assert len(captured_calls) == 1
        call = captured_calls[0]
        assert call["until"] == expected_until_utc
        assert call["budget"] is budget
        assert call["now"] == scheduler._clock

        # Notice delivered to notifier
        assert len(notifier) == 1
        assert notifier.last is not None
        assert notifier.last.data["until_utc"] == str(expected_until_utc)
        assert notifier.last.data["trials_run"] == 7
    finally:
        if orig_compile is not None:
            vn.compile_daily_digest = orig_compile
        elif hasattr(vn, "compile_daily_digest"):
            delattr(vn, "compile_daily_digest")


def test_daily_digest_counts_from_ledger_without_monkeypatch(tmp_path: Path):
    """Daily digest counts REJECTs and Trials directly from Ledger when
    compile_daily_digest is not imported (CCC-53, review finding)."""
    notifier = RecordingNotifier()
    budget = TokenBudget(daily_cap=100_000)
    budget.record_usage(12_345)
    desk = Desk(tmp_path)
    fam_id = desk.ledger.create_family("Digest Direct Count Family")
    hyp_id = desk.ledger.register_hypothesis(fam_id, EXAMPLE)
    with desk.ledger._lock:
        desk.ledger._db.execute(
            "INSERT INTO trials (hypothesis_id, verdict, closest_miss_json, cost_json, "
            "bars_count, bars_first, bars_last, bars_sha256, result_json, rules_version) "
            "VALUES (?, 'REJECT', '{\"check\": \"pf\"}', '{}', 100, 1000, 2000, 'abc', '{}', 1)",
            (hyp_id,),
        )
        desk.ledger._db.execute(
            "INSERT INTO trials (hypothesis_id, verdict, closest_miss_json, cost_json, "
            "bars_count, bars_first, bars_last, bars_sha256, result_json, rules_version) "
            "VALUES (?, 'CANDIDATE', NULL, '{}', 100, 1000, 2000, 'abc', '{}', 1)",
            (hyp_id,),
        )

    scheduler = Scheduler(
        root=tmp_path,
        desk=desk,
        notifier=notifier,
        budget=budget,
    )
    res = scheduler.run_job_now("daily_digest")
    assert res["status"] == "ok"
    assert res["trials"] == 2
    assert res["rejects"] == 1
    assert res["tokens_used"] == 12_345
    assert res["sent"] is True

    assert len(notifier) == 1
    notice = notifier.last
    assert notice is not None
    assert notice.kind == NoticeKind.DAILY_DIGEST
    assert notice.data["trials_run"] == 2
    assert notice.data["reject_count"] == 1
    assert notice.data["tokens_used"] == 12_345
    assert "1 REJECTs, 2 Trials run, 12,345 tokens used" in notice.text


def test_weekly_spread_floors_continues_on_symbol_cost_error(tmp_path: Path):
    """When cost measurement fails for one symbol, scheduler continues measuring
    remaining symbols."""
    from vibedesk.market.costs import CostError
    from vibedesk.market.fake_mt5 import FakeMt5

    class FlakyMt5(FakeMt5):
        def copy_ticks_range(self, symbol, start, end, flags):
            if symbol == "EURUSD":
                raise CostError("EURUSD: no ticks available")
            return super().copy_ticks_range(symbol, start, end, flags)

    source = SyntheticBarSource(1, date(2013, 12, 1), date(2027, 1, 1))
    fake_api = FlakyMt5(source)
    scheduler = Scheduler(
        root=tmp_path,
        api=fake_api,
        symbols=["EURUSD", "GOLD"],
    )
    res = scheduler.run_job_now("weekly_spread_floors")
    assert res["symbols"] == 1  # GOLD was measured despite EURUSD error
    assert "EURUSD" in res.get("errors", {})
