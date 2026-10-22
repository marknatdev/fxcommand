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
- If one bar touches both the stop and the target, its M1 bars decide which came first. Without
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

from vibedesk.engine.costs import CostSnapshot, swap_nights
from vibedesk.market.bars import Bars


class ExitReason(IntEnum):
    STOP = 1
    STOP_GAP = 2
    TARGET = 3
    END = 4


@dataclass(frozen=True, slots=True, eq=False)
class Signals:
    """Everything the engine needs per bar; every value is known at that bar's close."""

    long_entry: np.ndarray  # bool
    short_entry: np.ndarray  # bool
    stop_dist: np.ndarray  # price distance from the entry fill to the stop
    target_dist: np.ndarray  # price distance from the entry fill to the target; NaN for none


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
    the target are both touched; without it, or without M1 bars inside that bar, the stop wins."""
    n = len(bars)
    for name in ("long_entry", "short_entry", "stop_dist", "target_dist"):
        if len(getattr(signals, name)) != n:
            raise ValueError(f"signals.{name} has a different length than the bars")
    if m1 is None:
        m1_open = m1_high = m1_low = m1_spread = np.zeros(0)
        m1_lo = m1_hi = np.zeros(n, dtype=np.int64)
    else:
        m1_open, m1_high, m1_low = m1.open, m1.high, m1.low
        m1_spread = cost.spread_prices(m1.spread)
        m1_lo = np.searchsorted(m1.time, bars.time).astype(np.int64)
        m1_hi = np.searchsorted(m1.time, bars.time + bars.timeframe.seconds).astype(np.int64)

    table = _run(
        bars.open,
        bars.high,
        bars.low,
        bars.close,
        cost.spread_prices(bars.spread),
        np.asarray(signals.long_entry, dtype=np.bool_),
        np.asarray(signals.short_entry, dtype=np.bool_),
        np.asarray(signals.stop_dist, dtype=np.float64),
        np.asarray(signals.target_dist, dtype=np.float64),
        cost.slippage_price,
        m1_open,
        m1_high,
        m1_low,
        m1_spread,
        m1_lo,
        m1_hi,
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
    nights = swap_nights(records["entry_time"], records["exit_time"], cost.triple_swap_day)
    rate = np.where(side == 1, cost.swap_long_points, cost.swap_short_points)
    records["swap_r"] = rate * nights * cost.point / risk
    records["r"] = table[:, _R] + records["swap_r"]
    return Trades(records)


_STOP, _STOP_GAP, _TARGET, _END = 1, 2, 3, 4


@njit(cache=True)
def _check(pos, open_, high, low, spread, stop, target, slip):
    """One bar of an open position: (reason, fill, exit cost, stop-and-target conflict).

    Reason 0 means the position stays open. Longs exit on the bid; shorts on the ask. A stop
    that is already breached at the open exits at that open, which on the entry bar catches a
    stop placed inside the spread. When the bar touches both levels the stop is returned, with
    the conflict flag set so the caller can look at M1.
    """
    has_target = not np.isnan(target)
    if pos == 1:
        if open_ <= stop:
            return _STOP_GAP, open_ - slip, slip, False
        if has_target and open_ >= target:
            return _TARGET, target - slip, slip, False
        hit_target = has_target and high >= target
        if low <= stop:
            return _STOP, stop - slip, slip, hit_target
        if hit_target:
            return _TARGET, target - slip, slip, False
        return 0, 0.0, 0.0, False
    exit_cost = spread + slip
    ask_open = open_ + spread
    if ask_open >= stop:
        return _STOP_GAP, ask_open + slip, exit_cost, False
    if has_target and ask_open <= target:
        return _TARGET, target + slip, exit_cost, False
    hit_target = has_target and low + spread <= target
    if high + spread >= stop:
        return _STOP, stop + slip, exit_cost, hit_target
    if hit_target:
        return _TARGET, target + slip, exit_cost, False
    return 0, 0.0, 0.0, False


# Columns of the trade table _run returns; simulate() turns it into TRADE_DTYPE records.
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
    slip,
    m1_open,
    m1_high,
    m1_low,
    m1_spread,
    m1_lo,
    m1_hi,
):
    n = len(open_)
    out = np.zeros((n, 11))
    count = 0

    pos = 0  # +1 long, -1 short, 0 flat
    pending = 0  # side to enter at this bar's open
    opened = 0
    entry = 0.0
    stop = 0.0
    target = np.nan
    dist = 0.0
    entry_cost = 0.0

    for t in range(n):
        if pending != 0:
            pos = pending
            pending = 0
            opened = t
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
            why, fill, exit_cost, conflict = _check(
                pos, open_[t], high[t], low[t], spread[t], stop, target, slip
            )
            if conflict:
                # Walk the minutes inside this bar; the first one that decides wins.
                for k in range(m1_lo[t], m1_hi[t]):
                    minute = _check(
                        pos, m1_open[k], m1_high[k], m1_low[k], m1_spread[k], stop, target, slip
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

        if pos == 0 and t + 1 < n and stop_dist[t] > 0:
            if long_entry[t] and not short_entry[t]:
                pending = 1
            elif short_entry[t] and not long_entry[t]:
                pending = -1

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
