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
- If one bar touches both the stop and the target, the stop wins.
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
        ("r", "f8"),  # result in R, after every cost
        ("cost_r", "f8"),  # spread and slippage paid, in R
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


def simulate(bars: Bars, signals: Signals, cost: CostSnapshot) -> Trades:
    n = len(bars)
    for name in ("long_entry", "short_entry", "stop_dist", "target_dist"):
        if len(getattr(signals, name)) != n:
            raise ValueError(f"signals.{name} has a different length than the bars")
    columns = _run(
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
    )
    side, entry_idx, exit_idx, entry, exit_, stop, target, risk, r, cost_r, reason = columns
    records = np.empty(len(side), dtype=TRADE_DTYPE)
    records["side"] = side
    records["entry_idx"] = entry_idx
    records["exit_idx"] = exit_idx
    records["entry_time"] = bars.time[entry_idx]
    records["exit_time"] = bars.time[exit_idx]
    records["entry_price"] = entry
    records["exit_price"] = exit_
    records["stop_price"] = stop
    records["target_price"] = target
    records["risk"] = risk
    records["r"] = r
    records["cost_r"] = cost_r
    records["exit_reason"] = reason
    return Trades(records)


_STOP, _STOP_GAP, _TARGET, _END = 1, 2, 3, 4


@njit(cache=True)
def _run(open_, high, low, close, spread, long_entry, short_entry, stop_dist, target_dist, slip):
    n = len(open_)
    side = np.zeros(n, np.int8)
    entry_idx = np.zeros(n, np.int64)
    exit_idx = np.zeros(n, np.int64)
    entry_price = np.zeros(n)
    exit_price = np.zeros(n)
    stop_price = np.zeros(n)
    target_price = np.zeros(n)
    risk = np.zeros(n)
    r = np.zeros(n)
    cost_r = np.zeros(n)
    reason = np.zeros(n, np.int8)
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
            has_target = not np.isnan(target)
            why = 0
            fill = 0.0
            exit_cost = 0.0
            # On the entry bar the open can't be beyond the target (it lies on the far side of
            # the fill), but it can already be beyond a stop that sits inside the spread.
            if pos == 1:
                exit_cost = slip
                if open_[t] <= stop:
                    why, fill = _STOP_GAP, open_[t] - slip
                elif has_target and open_[t] >= target:
                    why, fill = _TARGET, target - slip
                elif low[t] <= stop:
                    why, fill = _STOP, stop - slip
                elif has_target and high[t] >= target:
                    why, fill = _TARGET, target - slip
            else:
                exit_cost = spread[t] + slip
                ask_open = open_[t] + spread[t]
                if ask_open >= stop:
                    why, fill = _STOP_GAP, ask_open + slip
                elif has_target and ask_open <= target:
                    why, fill = _TARGET, target + slip
                elif high[t] + spread[t] >= stop:
                    why, fill = _STOP, stop + slip
                elif has_target and low[t] + spread[t] <= target:
                    why, fill = _TARGET, target + slip
            if why != 0:
                side[count] = pos
                entry_idx[count] = opened
                exit_idx[count] = t
                entry_price[count] = entry
                exit_price[count] = fill
                stop_price[count] = stop
                target_price[count] = target
                risk[count] = dist
                r[count] = pos * (fill - entry) / dist
                cost_r[count] = (entry_cost + exit_cost) / dist
                reason[count] = why
                count += 1
                pos = 0

        if pos == 0 and t + 1 < n and stop_dist[t] > 0:
            if long_entry[t] and not short_entry[t]:
                pending = 1
            elif short_entry[t] and not long_entry[t]:
                pending = -1

    if pos != 0:
        t = n - 1
        if pos == 1:
            fill = close[t] - slip
            exit_cost = slip
        else:
            fill = close[t] + spread[t] + slip
            exit_cost = spread[t] + slip
        side[count] = pos
        entry_idx[count] = opened
        exit_idx[count] = t
        entry_price[count] = entry
        exit_price[count] = fill
        stop_price[count] = stop
        target_price[count] = target
        risk[count] = dist
        r[count] = pos * (fill - entry) / dist
        cost_r[count] = (entry_cost + exit_cost) / dist
        reason[count] = _END
        count += 1

    return (
        side[:count],
        entry_idx[:count],
        exit_idx[:count],
        entry_price[:count],
        exit_price[:count],
        stop_price[:count],
        target_price[:count],
        risk[:count],
        r[:count],
        cost_r[:count],
        reason[:count],
    )
