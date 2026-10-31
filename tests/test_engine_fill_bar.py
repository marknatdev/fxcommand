"""Tests for CCC-71 (fill-bar rule), CCC-79 (Holdout open-trade), and CCC-80 (atr_target)."""

from __future__ import annotations

import logging

import numpy as np
import pytest

from vibedesk.blocks.exits import AtrTargetParams, atr_target
from vibedesk.blocks.registry import LIBRARY
from vibedesk.engine.costs import CostSnapshot
from vibedesk.engine.simulate import ExitReason, Signals, simulate
from vibedesk.market.bars import Bars, Timeframe

from reference_engine import simulate_reference


def _make_bars(n: int = 10, spread_pts: int = 5) -> Bars:
    return Bars(
        symbol="GOLD",
        timeframe=Timeframe.H1,
        point=0.01,
        digits=2,
        time=[1704067200 + 3600 * i for i in range(n)],
        open=[100.0 + 0.1 * i for i in range(n)],
        high=[101.0 + 0.1 * i for i in range(n)],
        low=[99.0 + 0.1 * i for i in range(n)],
        close=[100.5 + 0.1 * i for i in range(n)],
        spread=[spread_pts] * n,
    )


def _make_cost() -> CostSnapshot:
    return CostSnapshot(point=0.01, spread_floor_points=50, slippage_points=1)


# =====================================================================
# CCC-71: Fill-Bar Rule Tests
# =====================================================================


def test_fill_bar_satisfied_both_bars_enters_normally():
    """Signal at bar 1, fill at bar 2. Both bar 1 and bar 2 satisfy time filter -> enters."""
    bars = _make_bars(5)
    cost = _make_cost()
    n = len(bars)

    long_entry = np.zeros(n, dtype=bool)
    long_entry[1] = True
    stop_dist = np.full(n, 2.0)
    target_dist = np.full(n, 5.0)

    time_filter = np.ones(n, dtype=bool)  # All true
    signals = Signals(
        long_entry=long_entry,
        short_entry=np.zeros(n, dtype=bool),
        stop_dist=stop_dist,
        target_dist=target_dist,
        time_filter=time_filter,
    )

    trades = simulate(bars, signals, cost)
    ref_trades = simulate_reference(bars, signals, cost, time_filter=time_filter)

    assert trades.skipped_fills == 0
    assert len(trades) == 1
    assert len(ref_trades) == 1
    assert trades[0]["entry_idx"] == 2
    assert ref_trades[0]["entry_idx"] == 2
    assert trades[0]["side"] == 1


def test_fill_bar_violates_fill_bar_skips_entry():
    """Signal at bar 1 (valid), but fill bar 2 violates time filter -> entry skipped."""
    bars = _make_bars(5)
    cost = _make_cost()
    n = len(bars)

    long_entry = np.zeros(n, dtype=bool)
    long_entry[1] = True
    stop_dist = np.full(n, 2.0)
    target_dist = np.full(n, 5.0)

    time_filter = np.ones(n, dtype=bool)
    time_filter[2] = False  # Bar 2 (fill bar) violates filter

    signals = Signals(
        long_entry=long_entry,
        short_entry=np.zeros(n, dtype=bool),
        stop_dist=stop_dist,
        target_dist=target_dist,
        time_filter=time_filter,
    )

    trades = simulate(bars, signals, cost)
    ref_trades = simulate_reference(bars, signals, cost, time_filter=time_filter)

    assert trades.skipped_fills == 1
    assert len(trades) == 0
    assert len(ref_trades) == 0


def test_fill_bar_violates_signal_bar_no_signal():
    """Signal bar 1 violates time filter -> no signal queued, 0 skips, 0 trades."""
    bars = _make_bars(5)
    cost = _make_cost()
    n = len(bars)

    long_entry = np.zeros(n, dtype=bool)
    long_entry[1] = True
    stop_dist = np.full(n, 2.0)
    target_dist = np.full(n, 5.0)

    time_filter = np.ones(n, dtype=bool)
    time_filter[1] = False  # Bar 1 (signal bar) violates filter

    signals = Signals(
        long_entry=long_entry,
        short_entry=np.zeros(n, dtype=bool),
        stop_dist=stop_dist,
        target_dist=target_dist,
        time_filter=time_filter,
    )

    trades = simulate(bars, signals, cost)
    ref_trades = simulate_reference(bars, signals, cost, time_filter=time_filter)

    assert trades.skipped_fills == 0
    assert len(trades) == 0
    assert len(ref_trades) == 0


def test_fill_bar_short_side_skip():
    """Short signal at bar 1 with bar 2 violating filter is skipped."""
    bars = _make_bars(5)
    cost = _make_cost()
    n = len(bars)

    short_entry = np.zeros(n, dtype=bool)
    short_entry[1] = True
    stop_dist = np.full(n, 2.0)
    target_dist = np.full(n, 5.0)

    time_filter = np.ones(n, dtype=bool)
    time_filter[2] = False

    signals = Signals(
        long_entry=np.zeros(n, dtype=bool),
        short_entry=short_entry,
        stop_dist=stop_dist,
        target_dist=target_dist,
        time_filter=time_filter,
    )

    trades = simulate(bars, signals, cost)
    ref_trades = simulate_reference(bars, signals, cost, time_filter=time_filter)

    assert trades.skipped_fills == 1
    assert len(trades) == 0
    assert len(ref_trades) == 0


def test_fill_bar_logging(caplog):
    """Verify that skipped fills are logged at INFO level."""
    bars = _make_bars(5)
    cost = _make_cost()
    n = len(bars)

    long_entry = np.zeros(n, dtype=bool)
    long_entry[1] = True
    stop_dist = np.full(n, 2.0)
    target_dist = np.full(n, 5.0)

    time_filter = np.ones(n, dtype=bool)
    time_filter[2] = False

    signals = Signals(
        long_entry=long_entry,
        short_entry=np.zeros(n, dtype=bool),
        stop_dist=stop_dist,
        target_dist=target_dist,
        time_filter=time_filter,
    )

    with caplog.at_level(logging.INFO):
        simulate(bars, signals, cost)

    assert "Fill-bar rule (CCC-71): skipped 1 fill(s)" in caplog.text


def test_fill_bar_dimension_mismatch():
    """Passing time_filter of different length raises ValueError."""
    bars = _make_bars(5)
    cost = _make_cost()
    n = len(bars)

    signals = Signals(
        long_entry=np.zeros(n, dtype=bool),
        short_entry=np.zeros(n, dtype=bool),
        stop_dist=np.full(n, 2.0),
        target_dist=np.full(n, 5.0),
        time_filter=np.ones(n + 1, dtype=bool),
    )

    with pytest.raises(ValueError, match="signals.time_filter has a different length"):
        simulate(bars, signals, cost)


def test_fill_bar_twin_agreement():
    """Random pattern of time filters and signals agrees perfectly between simulate and ref."""
    bars = _make_bars(20)
    cost = _make_cost()
    n = len(bars)

    rng = np.random.default_rng(42)
    long_entry = rng.random(n) > 0.7
    short_entry = rng.random(n) > 0.7
    time_filter = rng.random(n) > 0.4
    stop_dist = np.full(n, 1.5)
    target_dist = np.full(n, 3.0)

    signals = Signals(
        long_entry=long_entry,
        short_entry=short_entry,
        stop_dist=stop_dist,
        target_dist=target_dist,
        time_filter=time_filter,
    )

    sim = simulate(bars, signals, cost)
    ref = simulate_reference(bars, signals, cost, time_filter=time_filter)

    assert len(sim) == len(ref)
    for s, r in zip(sim.as_dicts(), ref, strict=True):
        assert s["side"] == r["side"]
        assert s["entry_idx"] == r["entry_idx"]
        assert s["exit_idx"] == r["exit_idx"]
        assert pytest.approx(s["entry_price"], abs=1e-6) == r["entry_price"]
        assert pytest.approx(s["exit_price"], abs=1e-6) == r["exit_price"]
        assert s["exit_reason"] == r["exit_reason"]
        assert pytest.approx(s["r"], abs=1e-6) == r["r"]


# =====================================================================
# CCC-79: Holdout Open Trade Settlement Tests
# =====================================================================


def test_holdout_open_trade_settled_at_final_bar_close_long():
    """Long trade remains open until end of data: settled at close[-1] with full exit cost."""
    bars = _make_bars(5, spread_pts=10)
    cost = _make_cost()
    n = len(bars)

    long_entry = np.zeros(n, dtype=bool)
    long_entry[0] = True
    # Stop and target far away so trade stays open
    stop_dist = np.full(n, 50.0)
    target_dist = np.full(n, 100.0)

    signals = Signals(
        long_entry=long_entry,
        short_entry=np.zeros(n, dtype=bool),
        stop_dist=stop_dist,
        target_dist=target_dist,
    )

    trades = simulate(bars, signals, cost)
    ref_trades = simulate_reference(bars, signals, cost)

    assert len(trades) == 1
    assert len(ref_trades) == 1
    t = trades.as_dicts()[0]
    r = ref_trades[0]

    last = n - 1
    assert t["exit_idx"] == last
    assert t["exit_reason"] is ExitReason.END
    # Long exit: bid close minus slippage
    expected_exit_price = bars.close[last] - cost.slippage_points * cost.point
    assert pytest.approx(t["exit_price"]) == expected_exit_price
    assert pytest.approx(r["exit_price"]) == expected_exit_price
    # Exit cost charged full slippage, round trip includes entry spread + 2*slip
    expected_cost = max(bars.spread[1], cost.spread_floor_points) * cost.point + 2 * (
        cost.slippage_points * cost.point
    )
    assert pytest.approx(t["cost_r"] * t["risk"]) == expected_cost
    assert pytest.approx(t["r"]) == r["r"]


def test_holdout_open_trade_settled_at_final_bar_close_short():
    """Short trade remains open until end of data: settled at close[-1] with full exit cost."""
    bars = _make_bars(5, spread_pts=10)
    cost = _make_cost()
    n = len(bars)

    short_entry = np.zeros(n, dtype=bool)
    short_entry[0] = True
    stop_dist = np.full(n, 50.0)
    target_dist = np.full(n, 100.0)

    signals = Signals(
        long_entry=np.zeros(n, dtype=bool),
        short_entry=short_entry,
        stop_dist=stop_dist,
        target_dist=target_dist,
    )

    trades = simulate(bars, signals, cost)
    ref_trades = simulate_reference(bars, signals, cost)

    assert len(trades) == 1
    assert len(ref_trades) == 1
    t = trades.as_dicts()[0]
    r = ref_trades[0]

    last = n - 1
    assert t["exit_idx"] == last
    assert t["exit_reason"] is ExitReason.END
    # Short exit: ask close plus slippage
    spread_at_last = max(bars.spread[last], cost.spread_floor_points) * cost.point
    expected_exit_price = bars.close[last] + spread_at_last + cost.slippage_points * cost.point
    assert pytest.approx(t["exit_price"]) == expected_exit_price
    assert pytest.approx(r["exit_price"]) == expected_exit_price
    # Total cost includes exit spread + 2*slip
    expected_cost = spread_at_last + 2 * (cost.slippage_points * cost.point)
    assert pytest.approx(t["cost_r"] * t["risk"]) == expected_cost
    assert pytest.approx(t["r"]) == r["r"]


# =====================================================================
# CCC-80: atr_target Cleanup Tests
# =====================================================================


def test_atr_target_callable_without_stop_distance():
    """atr_target is callable with only (bars, params), ignoring stop distance."""
    bars = _make_bars(20)
    params = AtrTargetParams(period=5, mult=2.5)

    # Call with 2 args (new signature)
    target_new = atr_target(bars, params)
    assert isinstance(target_new, np.ndarray)
    assert len(target_new) == len(bars)
    assert np.all(np.isnan(target_new[:4]))
    assert np.all(target_new[4:] > 0)

    # Call with 3 args (backward compat / variadic)
    dummy_stop = np.full(len(bars), 999.0)
    target_old = atr_target(bars, params, dummy_stop)
    np.testing.assert_array_equal(target_new, target_old)


def test_atr_target_registered_in_library():
    """LIBRARY contains atr_target with AtrTargetParams."""
    block = LIBRARY.get("atr_target")
    assert block.name == "atr_target"
    assert block.params is AtrTargetParams
