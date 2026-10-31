"""Tests for Forward Arena core components (CCC-66).

Covers:
- Strategy stepping on closed bars read from MT5 wrapper
- Fill-bar time filter check at t+1 (both pass and fail/skip with logging)
- Dual fill tracks (backtest_cost vs live_spread) and divergence
- Virtual $10,000 PaperAccount sizing via fixed-risk block
- Gap detection and catch_up replay from cache on restart
- Owner controls (pause/resume/remove/stop_arena) audited in Ledger
- Enrolment eligibility (CANDIDATE / PASS only) via send_to_arena
- Scheduler registration of 10s closed-bar poll and 07:00 Night Shift brief
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from vibedesk.app.scheduler import Scheduler
from vibedesk.arena import (
    ArenaRuntime,
    ArenaStepResult,
    ArenaStrategy,
    StrategyStatus,
)
from vibedesk.arena.strategy import check_fill_bar_time_filters
from vibedesk.blocks.spec import StrategySpec, validate
from vibedesk.market.bars import Bars, Timeframe
from vibedesk.market.cache import write_chunk
from vibedesk.market.costs import CostMeasurement, record_cost
from vibedesk.market.fake_mt5 import FakeMt5
from vibedesk.market.synthetic import SyntheticBarSource
from vibedesk.notify import NoticeKind, RecordingNotifier


def _make_spec(
    name: str = "Test Strategy",
    symbol: str = "GOLD",
    timeframe: str = "H1",
    direction: str = "long",
    filters: list | None = None,
    risk_pct: float = 1.0,
) -> StrategySpec:
    return validate(
        {
            "name": name,
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": direction,
            "entry": {"block": "donchian_breakout", "params": {"period": 20}},
            "filters": filters or [],
            "exit": {
                "stop": {"block": "atr_stop", "params": {"mult": 2.0}},
                "target": {"block": "r_target", "params": {"r": 2.0}},
            },
            "sizing": {"block": "fixed_risk", "params": {"risk_pct": risk_pct}},
        }
    )


def _make_sample_bars(n: int = 30, base_time: int = 1700000000, step_sec: int = 3600) -> list[dict]:
    bars = []
    price = 2000.0
    for i in range(n):
        # Create an upward trend after bar 20 to trigger Donchian breakout
        if i >= 20:
            price += 10.0
        open_ = price
        high_ = price + 5.0
        low_ = price - 2.0
        close_ = price + 3.0
        bars.append(
            {
                "time": base_time + i * step_sec,
                "open": open_,
                "high": high_,
                "low": low_,
                "close": close_,
                "spread": 25,
            }
        )
    return bars


# --- Strategy Stepping & Account Sizing Tests ---


def test_arena_strategy_stepping_and_fixed_risk_sizing():
    spec = _make_spec(risk_pct=1.0)
    strat = ArenaStrategy(
        strategy_id=1,
        hypothesis_id=101,
        spec=spec,
        symbol="GOLD",
        timeframe="H1",
        initial_balance=10_000.0,
    )
    assert strat.risk_fraction == 0.01
    assert strat.account_backtest.balance == 10_000.0
    assert strat.account_live.balance == 10_000.0

    bars = _make_sample_bars(25)
    # Feed warm-up bars
    for b in bars[:20]:
        res = strat.step(b)
        assert isinstance(res, ArenaStepResult)
        assert res.trades_opened == 0

    # Feed breakout bar: triggers entry signal for next bar
    strat.step(bars[20])
    assert strat.pending_signal is not None
    assert strat.pending_signal["direction"] == 1

    # Feed next bar: fill executes at open
    res_fill = strat.step(bars[21])
    assert res_fill.trades_opened == 1
    assert strat.account_backtest.open_position is not None
    assert strat.account_live.open_position is not None

    pos = strat.account_backtest.open_position
    # Fixed risk sizing check: risk amount = 10,000 * 0.01 = 100.0 USD
    risk_amount = pos.size * pos.stop_distance
    assert abs(risk_amount - 100.0) < 1e-4


# --- Fill-Bar Rule Time Filter Tests (CCC-71, ADR 0008) ---


def test_fill_bar_session_window_filter(caplog):
    # London session: 08:00 to 16:00
    spec = _make_spec(
        filters=[
            {
                "block": "session_window",
                "params": {"zone": "london", "start": "08:00", "end": "16:00"},
            }
        ]
    )

    # 1. Bar t+1 inside session window (e.g. 10:00 London time)
    # In London winter (UTC+0), 2026-11-04 10:00 UTC is minute 600 (between 480 and 960)
    dt_valid = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
    t_valid = int(dt_valid.timestamp())
    ok, reason = check_fill_bar_time_filters(spec, t_valid)
    assert ok is True
    assert reason is None

    # 2. Bar t+1 outside session window (e.g. 20:00 London time)
    dt_invalid = datetime(2026, 11, 4, 20, 0, tzinfo=UTC)
    t_invalid = int(dt_invalid.timestamp())
    ok, reason = check_fill_bar_time_filters(spec, t_invalid)
    assert ok is False
    assert "session_window violated" in reason

    # Test that ArenaStrategy.step skips fill and logs warning
    strat = ArenaStrategy(1, 10, spec, "GOLD", "H1")
    strat.pending_signal = {
        "direction": 1,
        "stop_dist": 10.0,
        "target_dist": 20.0,
        "trail_dist": None,
    }

    with caplog.at_level(logging.INFO):
        res = strat.step({"time": t_invalid, "open": 2000.0, "close": 2005.0, "spread": 25})
        assert res.trades_opened == 0
        assert strat.pending_signal is None
        assert "Fill skipped for strategy #1" in caplog.text


def test_fill_bar_day_of_week_filter(caplog):
    # Only Tue, Wed, Thu allowed (Mon and Fri = False)
    spec = _make_spec(
        filters=[
            {
                "block": "day_of_week",
                "params": {"mon": False, "tue": True, "wed": True, "thu": True, "fri": False},
            }
        ]
    )

    # Monday open (server weekday 0)
    dt_mon = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    ok_mon, reason_mon = check_fill_bar_time_filters(spec, int(dt_mon.timestamp()))
    assert ok_mon is False
    assert "day_of_week violated" in reason_mon

    # Tuesday open (server weekday 1)
    dt_tue = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
    ok_tue, reason_tue = check_fill_bar_time_filters(spec, int(dt_tue.timestamp()))
    assert ok_tue is True
    assert reason_tue is None


# --- Dual Fill Tracks & Divergence Tests ---


def test_dual_fill_tracks_divergence():
    spec = _make_spec()
    strat = ArenaStrategy(
        strategy_id=1,
        hypothesis_id=10,
        spec=spec,
        symbol="GOLD",
        timeframe="H1",
        spread_floor=55.0,
    )
    # Plant a pending long signal
    strat.pending_signal = {
        "direction": 1,
        "stop_dist": 10.0,
        "target_dist": 20.0,
        "trail_dist": None,
    }

    # Step on fill bar with bar_spread = 20, but live_spread = 80
    # Backtest track uses max(20, 55) = 55 pts + 1 pt = 56 pts = 0.56
    # Live spread track uses live_spread 80 pts + 1 pt = 81 pts = 0.81
    bar_fill = {
        "time": 1700000000,
        "open": 2000.0,
        "high": 2030.0,
        "low": 1999.0,
        "close": 2025.0,
        "spread": 20,
    }
    strat.step(bar_fill, live_spread=80.0)

    pos_bt = (
        strat.account_backtest.closed_trades[-1]
        if strat.account_backtest.closed_trades
        else strat.account_backtest.open_position
    )
    pos_lv = (
        strat.account_live.closed_trades[-1]
        if strat.account_live.closed_trades
        else strat.account_live.open_position
    )

    assert pos_bt is not None
    assert pos_lv is not None
    # Verify fill price divergence
    assert pos_bt.entry_price < pos_lv.entry_price
    assert round(pos_bt.entry_price - 2000.0, 2) == 0.56
    assert round(pos_lv.entry_price - 2000.0, 2) == 0.81


# --- Gap Detection and Catch-Up Replay Tests ---


def test_gap_detection_and_catch_up_replay(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir(parents=True)

    spec = _make_spec()
    runtime = ArenaRuntime(root=root, ledger=None)
    strat = ArenaStrategy(
        strategy_id=1,
        hypothesis_id=10,
        spec=spec,
        symbol="GOLD",
        timeframe="H1",
    )
    runtime.register_strategy(strat)

    base_time = 1767225600  # 2026-01-01
    # Strategy processed up to bar 5
    initial_bars = _make_sample_bars(5, base_time=base_time)
    for b in initial_bars:
        strat.step(b)
    assert strat.last_bar_time == initial_bars[-1]["time"]

    # Backend went offline. Parquet cache received bars 0 to 10
    all_bars = _make_sample_bars(10, base_time=base_time)
    bars_obj = Bars(
        symbol="GOLD",
        timeframe=Timeframe.H1,
        point=0.01,
        digits=2,
        time=np.array([b["time"] for b in all_bars], dtype=np.int64),
        open=np.array([b["open"] for b in all_bars], dtype=np.float64),
        high=np.array([b["high"] for b in all_bars], dtype=np.float64),
        low=np.array([b["low"] for b in all_bars], dtype=np.float64),
        close=np.array([b["close"] for b in all_bars], dtype=np.float64),
        spread=np.array([b["spread"] for b in all_bars], dtype=np.int32),
    )
    write_chunk(root, bars_obj, 2026)

    # Runtime recovers from restart
    replayed = runtime.check_and_recover_gaps()
    assert replayed == 5
    assert strat.last_bar_time == all_bars[-1]["time"]

    # Verify action recorded
    assert len(runtime.controls.actions) == 1
    assert runtime.controls.actions[0]["action"] == "catch_up"
    assert "replayed 5 missed bars" in runtime.controls.actions[0]["reason"]


# --- Owner Controls Tests ---


def test_arena_controls_lifecycle():
    spec = _make_spec()
    runtime = ArenaRuntime(root=Path("."), ledger=None)
    strat = ArenaStrategy(1, 10, spec, "GOLD", "H1")
    runtime.register_strategy(strat)
    controls = runtime.controls

    # 1. Pause
    controls.pause_strategy(1, reason="market holiday")
    assert strat.status == StrategyStatus.PAUSED
    assert controls.actions[-1]["action"] == "paused"

    # Paused strategy ignores new entries
    strat.pending_signal = {
        "direction": 1,
        "stop_dist": 10.0,
        "target_dist": 20.0,
        "trail_dist": None,
    }
    res = strat.step({"time": 1000, "open": 2000.0, "close": 2005.0})
    assert res.trades_opened == 0
    assert strat.pending_signal is None

    # 2. Resume
    controls.resume_strategy(1)
    assert strat.status == StrategyStatus.ACTIVE
    assert controls.actions[-1]["action"] == "resumed"

    # 3. Emergency stop
    controls.stop_arena(reason="high volatility spike")
    assert runtime.is_stopped() is True
    assert strat.status == StrategyStatus.PAUSED
    assert controls.actions[-1]["action"] == "stopped"

    # 4. Resume arena
    controls.resume_arena(reason="volatility normalised")
    assert runtime.is_stopped() is False
    assert strat.status == StrategyStatus.ACTIVE
    assert controls.actions[-1]["action"] == "resumed"

    # 5. Remove (terminal)
    controls.remove_strategy(1, reason="degraded edge")
    assert strat.status == StrategyStatus.REMOVED
    assert controls.actions[-1]["action"] == "removed"

    # Removed strategy cannot be resumed (terminal state)
    with pytest.raises(ValueError, match="cannot be resumed"):
        controls.resume_strategy(1)


# --- Enrolment & Eligibility Tests ---


def test_send_to_arena_eligibility_checks():
    spec = _make_spec()
    mock_ledger = MagicMock()

    # Reject verdict cannot enter
    reject_trial = MagicMock(verdict="REJECT", id=1)
    mock_ledger.trials_for.return_value = [reject_trial]
    mock_ledger.holdout_spend.return_value = None

    runtime = ArenaRuntime(root=Path("."), ledger=mock_ledger)
    with pytest.raises(ValueError, match="Only CANDIDATE or Holdout PASS"):
        runtime.send_to_arena(hypothesis_id=5)

    # Candidate verdict can enter
    candidate_trial = MagicMock(verdict="CANDIDATE", id=2)
    mock_ledger.trials_for.return_value = [candidate_trial]
    hyp_mock = MagicMock(spec_json=spec.model_dump_json(), symbol="GOLD", timeframe="H1")
    mock_ledger.hypothesis.return_value = hyp_mock
    mock_ledger.insert_arena_strategy.return_value = 1

    enrolled = runtime.send_to_arena(hypothesis_id=5)
    assert enrolled.id == 1
    assert enrolled.hypothesis_id == 5
    assert enrolled.status == StrategyStatus.ACTIVE
    assert enrolled.account_backtest.balance == 10_000.0
    assert enrolled.account_live.balance == 10_000.0


def test_send_to_arena_enforces_eligibility_even_with_explicit_spec():
    spec = _make_spec()
    mock_ledger = MagicMock()
    # Only REJECT trials
    reject_trial = MagicMock(verdict="REJECT", id=1, family_id=10)
    mock_ledger.trials_for.return_value = [reject_trial]
    mock_ledger.holdout_spend.return_value = None

    runtime = ArenaRuntime(root=Path("."), ledger=mock_ledger)
    with pytest.raises(ValueError, match="Only CANDIDATE or Holdout PASS"):
        runtime.send_to_arena(hypothesis_id=5, spec=spec)


def test_send_to_arena_holdout_pass_with_family_id():
    spec = _make_spec()
    mock_ledger = MagicMock()
    # Trial verdict is REJECT, but holdout spend for family_id 42 is PASS
    trial = MagicMock(verdict="REJECT", id=1, family_id=42)
    mock_ledger.trials_for.return_value = [trial]
    hyp_mock = MagicMock(
        family_id=42,
        spec_json=spec.model_dump_json(),
        symbol="GOLD",
        timeframe="H1",
    )
    mock_ledger.hypothesis.return_value = hyp_mock
    mock_ledger.holdout_spend.side_effect = lambda fam: (
        MagicMock(verdict="PASS") if fam == 42 else None
    )
    mock_ledger.insert_arena_strategy.return_value = 1

    runtime = ArenaRuntime(root=Path("."), ledger=mock_ledger)
    enrolled = runtime.send_to_arena(hypothesis_id=5)
    assert enrolled.id == 1
    mock_ledger.holdout_spend.assert_called_with(42)


def test_paused_strategy_manages_open_position_to_stop():
    spec = _make_spec()
    runtime = ArenaRuntime(root=Path("."), ledger=None)
    strat = ArenaStrategy(
        strategy_id=1,
        hypothesis_id=10,
        spec=spec,
        symbol="GOLD",
        timeframe="H1",
    )
    runtime.register_strategy(strat)

    # 1. Step bar to fill a long position: entry at 2000.0, stop at 1990.0
    strat.pending_signal = {
        "direction": 1,
        "stop_dist": 10.0,
        "target_dist": 20.0,
        "trail_dist": None,
    }
    bar_fill = {"time": 1000, "open": 2000.0, "high": 2005.0, "low": 1998.0, "close": 2002.0}
    strat.step(bar_fill)
    assert strat.account_backtest.open_position is not None

    # 2. Pause strategy
    runtime.controls.pause_strategy(1, reason="owner pause")
    assert strat.status == StrategyStatus.PAUSED

    # 3. Bar dips to 1985.0 (breaches stop at 1990.0)
    bar_stop = {"time": 2000, "open": 2000.0, "high": 2001.0, "low": 1985.0, "close": 1988.0}
    res = runtime.process_closed_bar("GOLD", "H1", bar_stop)
    assert len(res) == 1
    assert res[0].trades_closed == 1
    assert strat.account_backtest.open_position is None
    assert len(strat.account_backtest.closed_trades) == 1
    assert strat.account_backtest.closed_trades[0].exit_reason == "stop"


def test_poll_closed_bars_deduplicates_and_subsequent_signals_fire(tmp_path: Path):
    source = SyntheticBarSource(1, date(2026, 1, 1), date(2026, 12, 31))
    now = datetime(2026, 1, 7, 12, 0, tzinfo=UTC)
    fake_mt5 = FakeMt5(source, now=now)
    assert fake_mt5.initialize()

    spec = _make_spec()
    runtime = ArenaRuntime(root=tmp_path, ledger=None, api=fake_mt5)
    strat = ArenaStrategy(1, 10, spec, "GOLD", "H1")
    runtime.register_strategy(strat)

    # First poll steps closed bars
    res1 = runtime.poll_closed_bars(root=tmp_path, api=fake_mt5, clock=lambda: now)
    assert len(res1) > 0
    history_len = len(strat._bars_history)
    last_time = strat.last_bar_time
    assert last_time > 0

    # Second poll with identical clock time no-ops (deduplicates, no re-stepping)
    runtime.poll_closed_bars(root=tmp_path, api=fake_mt5, clock=lambda: now)
    assert len(strat._bars_history) == history_len
    assert strat.last_bar_time == last_time


def test_arena_strategy_loads_measured_costs(tmp_path: Path):
    meas = CostMeasurement(
        symbol="GOLD",
        measured_at="2026-10-25T00:00:00Z",
        point=0.01,
        spread_floor_points=65.0,
        spread_p90_points=70.0,
        ticks=1000,
        swap_long_points=-92.0,
        swap_short_points=25.0,
        triple_swap_day=4,
    )
    record_cost(tmp_path, meas)

    spec = _make_spec()
    runtime = ArenaRuntime(root=tmp_path, ledger=None)
    strat = runtime.send_to_arena(hypothesis_id=1, spec=spec, symbol="GOLD", timeframe="H1")

    assert strat.spread_floor == 65.0
    assert strat.swap_long_points == -92.0
    assert strat.triple_day == 4


# --- Scheduler 10s Poll and 07:00 Brief Tests ---


def test_scheduler_arena_closed_bar_poll_and_night_shift_brief(tmp_path: Path):
    notifier = RecordingNotifier()
    source = SyntheticBarSource(1, date(2026, 1, 1), date(2026, 12, 31))
    now = datetime(2026, 11, 4, 7, 0, tzinfo=UTC)
    fake_mt5 = FakeMt5(source, now=now)

    runtime = ArenaRuntime(root=tmp_path, ledger=None, api=fake_mt5)
    spec = _make_spec()
    strat = ArenaStrategy(1, 10, spec, "GOLD", "H1")
    runtime.register_strategy(strat)

    scheduler = Scheduler(
        root=tmp_path,
        api=fake_mt5,
        notifier=notifier,
        arena_runtime=runtime,
        clock=lambda: now,
    )
    # Check that both Arena jobs are registered
    job_names = {j.name for j in scheduler.jobs()}
    assert "arena_closed_bar_poll" in job_names
    assert "night_shift_brief" in job_names

    # Run arena closed bar poll immediately
    res_poll = scheduler.run_job_now("arena_closed_bar_poll")
    assert res_poll["job"] == "arena_closed_bar_poll"
    assert res_poll["status"] == "ok"

    # Run night shift morning brief immediately
    res_brief = scheduler.run_job_now("night_shift_brief")
    assert res_brief["job"] == "night_shift_brief"
    assert res_brief["status"] == "ok"
    assert res_brief["sent"] is True

    # Verify Telegram notice arrived in RecordingNotifier
    assert len(notifier.notices) == 1
    notice = notifier.notices[0]
    assert notice.kind == NoticeKind.DAILY_DIGEST
    assert "Vibe Desk Night Shift Brief (07:00 Bangkok)" in notice.text
