"""Turn signals into trades measured in R, with spread, slippage and conservative fills.

Fill rules (spec §6.1):
- A signal is read at a bar's close; the trade enters at the next bar's open.
- Bars are bid prices. Longs buy at the ask (bid + spread) and sell at the bid. Shorts sell at
  the bid and buy back at the ask, so their stops and targets trigger on ask prices.
- Spread is max(bar spread, Spread Floor); slippage is charged on every fill.
- The stop and target are set from the entry fill, so R = (exit - entry) / risk for longs.
- A bar that opens beyond the stop exits at that open (a gap). That includes the entry bar
  itself: a stop closer than the spread is already breached when the trade fills, so it exits
  at the market, not at the stop. A bar that opens beyond the target exits at the target, not
  at the better open.
- A trailing stop is updated only on bar closes and only ever moves in the trade's favour. It
  replaces the initial stop once it is tighter.
- A time exit leaves at the open of the bar `time_bars` bars after the entry bar. An opposite
  entry signal (from the entry Block alone, ignoring filters and direction) leaves at the next
  open. Neither reverses the position.
- When several exits apply, the first in this order wins: stop, target, trailing stop, time,
  opposite signal.
- If one bar touches both a stop and the target, its M1 bars decide which came first. Without
  M1 data, or if the minutes don't settle it, the stop wins.
- Swap is charged for every server rollover the position is open through (see swap_nights).
- One position at a time. A trade that exits during a bar can be followed by a signal at that
  bar's close. Signals that fire while a position is open are ignored.
- A position still open after the last bar exits at that bar's close.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

import numpy as np
from numba import njit

from vibedesk.engine.costs import CostSnapshot
from vibedesk.engine.prepared import PreparedMarket, prepare
from vibedesk.market.bars import Bars


class ExitReason(IntEnum):
    STOP = 1
    STOP_GAP = 2
    TARGET = 3
    END = 4
    TRAIL = 5
    TIME = 6
    OPPOSITE = 7


@dataclass(frozen=True, slots=True, eq=False)
class Signals:
    """Everything the engine needs per bar; every value is known at that bar's close."""

    long_entry: np.ndarray  # bool
    short_entry: np.ndarray  # bool
    stop_dist: np.ndarray  # price distance from the entry fill to the stop
    target_dist: np.ndarray  # price distance from the entry fill to the target; NaN for none
    trail_dist: np.ndarray | None = None  # trailing distance from the close; None for no trail
    time_bars: int = 0  # bars to hold before a time exit; 0 for none
    exit_long: np.ndarray | None = None  # bool: close a long at the next open
    exit_short: np.ndarray | None = None  # bool: close a short at the next open


TRADE_DTYPE = np.dtype(
    [
        ("side", "i1"),  # +1 long, -1 short
        ("entry_idx", "i8"),
        ("exit_idx", "i8"),
        ("entry_time", "i8"),  # UTC epoch seconds of the entry bar
        ("exit_time", "i8"),
        ("entry_price", "f8"),
        ("exit_price", "f8"),
        ("stop_price", "f8"),
        ("target_price", "f8"),  # NaN when the trade had no target
        ("risk", "f8"),  # price distance from entry to stop: 1R
        ("r", "f8"),  # result in R, after every cost and swap
        ("cost_r", "f8"),  # spread and slippage paid, in R
        ("swap_r", "f8"),  # swap in R; negative when paid, positive when earned
        ("exit_reason", "i1"),
    ]
)


@dataclass(frozen=True, slots=True, eq=False)
class Trades:
    records: np.ndarray  # TRADE_DTYPE

    def __len__(self) -> int:
        return len(self.records)

    def __getitem__(self, field: str) -> np.ndarray:
        return self.records[field]

    def as_dicts(self) -> list[dict]:
        rows = []
        for record in self.records:
            row = {name: record[name].item() for name in TRADE_DTYPE.names}
            row["exit_reason"] = ExitReason(row["exit_reason"])
            rows.append(row)
        return rows


def simulate(bars: Bars, signals: Signals, cost: CostSnapshot, m1: Bars | None = None) -> Trades:
    """Simulate one Combination's signals. `m1`, when given, settles bars where the stop and
    the target are both touched; without it, or without M1 bars inside that bar, the stop wins.

    To simulate the same bars many times, prepare them once and call simulate_prepared.
    """
    return simulate_prepared(prepare(bars, cost, m1), signals)


def simulate_prepared(market: PreparedMarket, signals: Signals) -> Trades:
    """Simulate one Combination's signals on a market from `prepare`."""
    n = len(market)
    trail_dist = market.no_trail if signals.trail_dist is None else signals.trail_dist
    exit_long = market.never if signals.exit_long is None else signals.exit_long
    exit_short = market.never if signals.exit_short is None else signals.exit_short
    arrays = {
        "long_entry": signals.long_entry,
        "short_entry": signals.short_entry,
        "stop_dist": signals.stop_dist,
        "target_dist": signals.target_dist,
        "trail_dist": trail_dist,
        "exit_long": exit_long,
        "exit_short": exit_short,
    }
    for name, array in arrays.items():
        if len(array) != n:
            raise ValueError(f"signals.{name} has a different length than the bars")

    bars, cost = market.bars, market.cost
    table = _run(
        bars.open,
        bars.high,
        bars.low,
        bars.close,
        market.spread,
        np.asarray(signals.long_entry, dtype=np.bool_),
        np.asarray(signals.short_entry, dtype=np.bool_),
        np.asarray(signals.stop_dist, dtype=np.float64),
        np.asarray(signals.target_dist, dtype=np.float64),
        np.asarray(trail_dist, dtype=np.float64),
        int(signals.time_bars),
        np.asarray(exit_long, dtype=np.bool_),
        np.asarray(exit_short, dtype=np.bool_),
        market.slippage,
        market.m1_open,
        market.m1_high,
        market.m1_low,
        market.m1_spread,
        market.m1_lo,
        market.m1_hi,
    )
    entry_idx = table[:, _ENTRY_IDX].astype(np.int64)
    exit_idx = table[:, _EXIT_IDX].astype(np.int64)
    side = table[:, _SIDE].astype(np.int8)
    risk = table[:, _RISK]

    records = np.empty(len(table), dtype=TRADE_DTYPE)
    records["side"] = side
    records["entry_idx"] = entry_idx
    records["exit_idx"] = exit_idx
    records["entry_time"] = bars.time[entry_idx]
    records["exit_time"] = bars.time[exit_idx]
    records["entry_price"] = table[:, _ENTRY]
    records["exit_price"] = table[:, _EXIT]
    records["stop_price"] = table[:, _STOP_PRICE]
    records["target_price"] = table[:, _TARGET_PRICE]
    records["risk"] = risk
    records["cost_r"] = table[:, _COST_R]
    records["exit_reason"] = table[:, _WHY].astype(np.int8)

    # Swap never changes when a trade exits, so it is added after the fill loop.
    nights = market.rollovers[exit_idx] - market.rollovers[entry_idx]
    rate = np.where(side == 1, cost.swap_long_points, cost.swap_short_points)
    records["swap_r"] = rate * nights * cost.point / risk
    records["r"] = table[:, _R] + records["swap_r"]
    return Trades(records)


_STOP, _STOP_GAP, _TARGET, _END, _TRAIL, _TIME, _OPPOSITE = 1, 2, 3, 4, 5, 6, 7


@njit(cache=True)
def _check(pos, open_, high, low, spread, stop, target, trail, at_open, slip):
    """One bar of an open position: (reason, fill, exit cost, stop-and-target conflict).

    Reason 0 means the position stays open. Longs exit on the bid; shorts on the ask. `trail` is
    the trailing stop (NaN for none) and `at_open` a time or opposite exit due at this bar's
    open (0 for none). The open is checked first, in priority order: stop, target, trailing
    stop, then the exit due at the open. A stop already breached at the open exits there,
    which on the entry bar catches a stop placed inside the spread. Inside the bar, whichever
    stop is tighter applies. When the bar also touches the target, the stop is returned with
    the conflict flag set so the caller can look at M1.
    """
    has_target = not np.isnan(target)
    has_trail = not np.isnan(trail)
    if pos == 1:
        if open_ <= stop:
            return _STOP_GAP, open_ - slip, slip, False
        if has_target and open_ >= target:
            return _TARGET, target - slip, slip, False
        if has_trail and open_ <= trail:
            return _TRAIL, open_ - slip, slip, False
        if at_open != 0:
            return at_open, open_ - slip, slip, False
        level, why = stop, _STOP
        if has_trail and trail > stop:
            level, why = trail, _TRAIL
        hit_target = has_target and high >= target
        if low <= level:
            return why, level - slip, slip, hit_target
        if hit_target:
            return _TARGET, target - slip, slip, False
        return 0, 0.0, 0.0, False
    exit_cost = spread + slip
    ask_open = open_ + spread
    if ask_open >= stop:
        return _STOP_GAP, ask_open + slip, exit_cost, False
    if has_target and ask_open <= target:
        return _TARGET, target + slip, exit_cost, False
    if has_trail and ask_open >= trail:
        return _TRAIL, ask_open + slip, exit_cost, False
    if at_open != 0:
        return at_open, ask_open + slip, exit_cost, False
    level, why = stop, _STOP
    if has_trail and trail < stop:
        level, why = trail, _TRAIL
    hit_target = has_target and low + spread <= target
    if high + spread >= level:
        return why, level + slip, exit_cost, hit_target
    if hit_target:
        return _TARGET, target + slip, exit_cost, False
    return 0, 0.0, 0.0, False


# Columns of the trade table _run returns; simulate_prepared() turns it into TRADE_DTYPE records.
(
    _SIDE,
    _ENTRY_IDX,
    _EXIT_IDX,
    _ENTRY,
    _EXIT,
    _STOP_PRICE,
    _TARGET_PRICE,
    _RISK,
    _R,
    _COST_R,
    _WHY,
) = range(11)


@njit(cache=True)
def _record(out, k, pos, opened, t, entry, fill, stop, target, dist, total_cost, why):
    out[k, _SIDE] = pos
    out[k, _ENTRY_IDX] = opened
    out[k, _EXIT_IDX] = t
    out[k, _ENTRY] = entry
    out[k, _EXIT] = fill
    out[k, _STOP_PRICE] = stop
    out[k, _TARGET_PRICE] = target
    out[k, _RISK] = dist
    out[k, _R] = pos * (fill - entry) / dist
    out[k, _COST_R] = total_cost / dist
    out[k, _WHY] = why


@njit(cache=True)
def _run(
    open_,
    high,
    low,
    close,
    spread,
    long_entry,
    short_entry,
    stop_dist,
    target_dist,
    trail_dist,
    time_bars,
    exit_long,
    exit_short,
    slip,
    m1_open,
    m1_high,
    m1_low,
    m1_spread,
    m1_lo,
    m1_hi,
):
    n = len(open_)
    # Every trade enters on a bar after one that signals one side only, so that count bounds
    # the table; sizing it by the bars would allocate megabytes per call for a few hundred rows.
    capacity = 1
    first, last = n, -1  # the first and last bars that signal one side only
    for t in range(n):
        if long_entry[t] != short_entry[t]:
            capacity += 1
            if first == n:
                first = t
            last = t
    out = np.empty((capacity, 11))
    count = 0

    pos = 0  # +1 long, -1 short, 0 flat
    pending = 0  # side to enter at this bar's open
    opened = 0
    entry = 0.0
    stop = 0.0
    target = np.nan
    trail = np.nan
    opposite_due = False
    dist = 0.0
    entry_cost = 0.0

    # Nothing can happen before the first signal, or once flat after the last one, so a
    # simulation whose signals fill one year costs one year of bars, whatever the data's length.
    for t in range(first, n):
        if pending != 0:
            pos = pending
            pending = 0
            opened = t
            trail = np.nan
            opposite_due = False
            dist = stop_dist[t - 1]
            if pos == 1:
                entry = open_[t] + spread[t] + slip
                entry_cost = spread[t] + slip
                stop = entry - dist
                target = entry + target_dist[t - 1]
            else:
                entry = open_[t] - slip
                entry_cost = slip
                stop = entry + dist
                target = entry - target_dist[t - 1]

        if pos != 0:
            at_open = 0
            if time_bars > 0 and t - opened >= time_bars:
                at_open = _TIME
            elif opposite_due:
                at_open = _OPPOSITE
            why, fill, exit_cost, conflict = _check(
                pos, open_[t], high[t], low[t], spread[t], stop, target, trail, at_open, slip
            )
            if conflict:
                # Walk the minutes inside this bar; the first one that decides wins.
                for k in range(m1_lo[t], m1_hi[t]):
                    minute = _check(
                        pos,
                        m1_open[k],
                        m1_high[k],
                        m1_low[k],
                        m1_spread[k],
                        stop,
                        target,
                        trail,
                        0,
                        slip,
                    )
                    if minute[0] != 0:
                        why, fill, exit_cost = minute[0], minute[1], minute[2]
                        break
            if why != 0:
                _record(
                    out,
                    count,
                    pos,
                    opened,
                    t,
                    entry,
                    fill,
                    stop,
                    target,
                    dist,
                    entry_cost + exit_cost,
                    why,
                )
                count += 1
                pos = 0

        if pos != 0:
            # At this bar's close: ratchet the trailing stop and note an opposite signal.
            if pos == 1:
                level = close[t] - trail_dist[t]
                if not np.isnan(level) and (np.isnan(trail) or level > trail):
                    trail = level
                opposite_due = exit_long[t]
            else:
                level = close[t] + spread[t] + trail_dist[t]
                if not np.isnan(level) and (np.isnan(trail) or level < trail):
                    trail = level
                opposite_due = exit_short[t]

        if pos == 0 and t + 1 < n and stop_dist[t] > 0:
            if long_entry[t] and not short_entry[t]:
                pending = 1
            elif short_entry[t] and not long_entry[t]:
                pending = -1
        if pos == 0 and pending == 0 and t >= last:
            break

    if pos != 0:
        # Still open after the last bar: exit at its close.
        t = n - 1
        if pos == 1:
            fill, exit_cost = close[t] - slip, slip
        else:
            fill, exit_cost = close[t] + spread[t] + slip, spread[t] + slip
        _record(
            out,
            count,
            pos,
            opened,
            t,
            entry,
            fill,
            stop,
            target,
            dist,
            entry_cost + exit_cost,
            _END,
        )
        count += 1

    return out[:count]
