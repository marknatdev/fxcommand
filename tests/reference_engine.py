"""A slow, readable twin of vibedesk.engine.simulate, written separately to cross-check it.

It shares no code with the engine, so the two agreeing on random data is real evidence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from vibedesk.engine.simulate import ExitReason


@dataclass
class _Position:
    side: int
    entry_idx: int
    entry: float
    stop: float
    target: float | None
    risk: float
    entry_cost: float


def simulate_reference(bars, signals, cost) -> list[dict]:
    slip = cost.slippage_points * cost.point
    spreads = [max(int(s), cost.spread_floor_points) * cost.point for s in bars.spread]
    trades: list[dict] = []
    position: _Position | None = None
    waiting: int | None = None  # signal bar whose entry fills at the next open

    for t in range(len(bars)):
        bid = Quote(bars.open[t], bars.high[t], bars.low[t], bars.close[t])
        ask = bid.shifted(spreads[t])
        entered_now = False

        if waiting is not None:
            side = 1 if signals.long_entry[waiting] else -1
            risk = float(signals.stop_dist[waiting])
            target_dist = float(signals.target_dist[waiting])
            if side == 1:
                entry, entry_cost = ask.open + slip, spreads[t] + slip
            else:
                entry, entry_cost = bid.open - slip, slip
            position = _Position(
                side=side,
                entry_idx=t,
                entry=entry,
                stop=entry - side * risk,
                target=None if math.isnan(target_dist) else entry + side * target_dist,
                risk=risk,
                entry_cost=entry_cost,
            )
            waiting = None
            entered_now = True

        if position is not None:
            # Longs close by selling at the bid; shorts close by buying at the ask.
            quote = bid if position.side == 1 else ask
            exit_cost = slip if position.side == 1 else spreads[t] + slip
            outcome = _check_exit(position, quote, slip, target_gap_allowed=not entered_now)
            if outcome is not None:
                fill, reason = outcome
                trades.append(_close(position, t, fill, reason, exit_cost, bars))
                position = None

        if position is None and t + 1 < len(bars):
            go_long = bool(signals.long_entry[t]) and not bool(signals.short_entry[t])
            go_short = bool(signals.short_entry[t]) and not bool(signals.long_entry[t])
            if (go_long or go_short) and signals.stop_dist[t] > 0:
                waiting = t

    if position is not None:
        t = len(bars) - 1
        if position.side == 1:
            fill, exit_cost = bars.close[t] - slip, slip
        else:
            fill, exit_cost = bars.close[t] + spreads[t] + slip, spreads[t] + slip
        trades.append(_close(position, t, fill, ExitReason.END, exit_cost, bars))
    return trades


@dataclass
class Quote:
    open: float
    high: float
    low: float
    close: float

    def shifted(self, amount: float) -> Quote:
        return Quote(self.open + amount, self.high + amount, self.low + amount, self.close + amount)


def _check_exit(position: _Position, quote: Quote, slip: float, target_gap_allowed: bool):
    """Return (fill, reason) or None. `quote` is the side of the book the position exits on.

    The stop-gap check also runs on the entry bar: a stop inside the spread is breached the
    moment the trade fills, and the exit happens at the market price, not at the stop.
    """
    favourable = position.side  # +1: higher prices help a long; -1: lower prices help a short

    def beyond(price: float, level: float) -> bool:
        return (price - level) * favourable >= 0

    def adverse(price: float, level: float) -> bool:
        return (level - price) * favourable >= 0

    worst = quote.low if position.side == 1 else quote.high
    best = quote.high if position.side == 1 else quote.low
    if adverse(quote.open, position.stop):
        return quote.open - favourable * slip, ExitReason.STOP_GAP
    if target_gap_allowed and position.target is not None and beyond(quote.open, position.target):
        return position.target - favourable * slip, ExitReason.TARGET
    if adverse(worst, position.stop):
        return position.stop - favourable * slip, ExitReason.STOP
    if position.target is not None and beyond(best, position.target):
        return position.target - favourable * slip, ExitReason.TARGET
    return None


def _close(position: _Position, t: int, fill: float, reason, exit_cost: float, bars) -> dict:
    return dict(
        side=position.side,
        entry_idx=position.entry_idx,
        exit_idx=t,
        entry_time=int(bars.time[position.entry_idx]),
        exit_time=int(bars.time[t]),
        entry_price=position.entry,
        exit_price=fill,
        stop_price=position.stop,
        target_price=math.nan if position.target is None else position.target,
        risk=position.risk,
        r=position.side * (fill - position.entry) / position.risk,
        cost_r=(position.entry_cost + exit_cost) / position.risk,
        exit_reason=reason,
    )
