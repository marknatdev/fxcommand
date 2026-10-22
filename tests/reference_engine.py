"""A slow, readable twin of vibedesk.engine.simulate, written separately to cross-check it.

It shares no code with the engine (not even the swap-night counting or the server clock), so
the two agreeing on random data is real evidence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from vibedesk.engine.simulate import ExitReason

NEW_YORK = ZoneInfo("America/New_York")


@dataclass
class _Position:
    side: int
    entry_idx: int
    entry: float
    stop: float
    target: float | None
    risk: float
    entry_cost: float


@dataclass
class Quote:
    open: float
    high: float
    low: float
    close: float

    def shifted(self, amount: float) -> Quote:
        return Quote(self.open + amount, self.high + amount, self.low + amount, self.close + amount)


def simulate_reference(bars, signals, cost, m1=None) -> list[dict]:
    slip = cost.slippage_points * cost.point

    def spread_of(points) -> float:
        return max(int(points), cost.spread_floor_points) * cost.spread_multiplier * cost.point

    spreads = [spread_of(s) for s in bars.spread]
    trades: list[dict] = []
    position: _Position | None = None
    waiting: int | None = None  # signal bar whose entry fills at the next open

    for t in range(len(bars)):
        bid = Quote(bars.open[t], bars.high[t], bars.low[t], bars.close[t])
        entered_now = False

        if waiting is not None:
            side = 1 if signals.long_entry[waiting] else -1
            risk = float(signals.stop_dist[waiting])
            target_dist = float(signals.target_dist[waiting])
            if side == 1:
                entry, entry_cost = bid.open + spreads[t] + slip, spreads[t] + slip
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
            outcome = _check_exit(
                position, bid, spreads[t], slip, target_gap_allowed=not entered_now
            )
            if outcome is not None and outcome[2] and m1 is not None:
                outcome = _settle_with_m1(position, bars, t, m1, spread_of, slip) or outcome
            if outcome is not None:
                fill, reason, _, exit_cost = outcome
                trades.append(_close(position, t, fill, reason, exit_cost, bars, cost))
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
        trades.append(_close(position, t, fill, ExitReason.END, exit_cost, bars, cost))
    return trades


def _check_exit(position: _Position, bid: Quote, spread: float, slip: float, target_gap_allowed):
    """Return (fill, reason, both_touched, exit_cost) or None.

    Longs close by selling at the bid; shorts by buying at the ask. The stop-gap check also runs
    on the entry bar: a stop inside the spread is breached the moment the trade fills, and the
    exit happens at the market price, not at the stop.
    """
    quote = bid if position.side == 1 else bid.shifted(spread)
    exit_cost = slip if position.side == 1 else spread + slip
    favourable = position.side  # +1: higher prices help a long; -1: lower prices help a short

    def beyond(price: float, level: float) -> bool:
        return (price - level) * favourable >= 0

    def adverse(price: float, level: float) -> bool:
        return (level - price) * favourable >= 0

    worst = quote.low if position.side == 1 else quote.high
    best = quote.high if position.side == 1 else quote.low
    if adverse(quote.open, position.stop):
        return quote.open - favourable * slip, ExitReason.STOP_GAP, False, exit_cost
    if target_gap_allowed and position.target is not None and beyond(quote.open, position.target):
        return position.target - favourable * slip, ExitReason.TARGET, False, exit_cost
    reaches_target = position.target is not None and beyond(best, position.target)
    if adverse(worst, position.stop):
        return position.stop - favourable * slip, ExitReason.STOP, reaches_target, exit_cost
    if reaches_target:
        return position.target - favourable * slip, ExitReason.TARGET, False, exit_cost
    return None


def _settle_with_m1(position, bars, t, m1, spread_of, slip):
    """Replay the minutes inside bar t; the first minute that triggers an exit decides."""
    start, end = int(bars.time[t]), int(bars.time[t]) + bars.timeframe.seconds
    for k in range(len(m1)):
        if not start <= int(m1.time[k]) < end:
            continue
        minute = Quote(m1.open[k], m1.high[k], m1.low[k], m1.close[k])
        outcome = _check_exit(position, minute, spread_of(m1.spread[k]), slip, True)
        if outcome is not None:
            return outcome
    return None


def _close(position: _Position, t: int, fill: float, reason, exit_cost: float, bars, cost) -> dict:
    nights = _nights_held(int(bars.time[position.entry_idx]), int(bars.time[t]), cost)
    rate = cost.swap_long_points if position.side == 1 else cost.swap_short_points
    swap_r = rate * nights * cost.point / position.risk
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
        r=position.side * (fill - position.entry) / position.risk + swap_r,
        cost_r=(position.entry_cost + exit_cost) / position.risk,
        swap_r=swap_r,
        exit_reason=reason,
    )


def _server_clock(epoch: int) -> datetime:
    """Naive server-clock datetime: New York wall time plus seven hours."""
    new_york = datetime.fromtimestamp(epoch, UTC).astimezone(NEW_YORK)
    return new_york.replace(tzinfo=None) + timedelta(hours=7)


def _nights_held(entry_epoch: int, exit_epoch: int, cost) -> int:
    """Walk every server midnight after the entry, up to and including the exit bar's open."""
    entered, exited = _server_clock(entry_epoch), _server_clock(exit_epoch)
    triple_weekday = (cost.triple_swap_day - 1) % 7  # MT5 counts from Sunday; Python from Monday
    nights = 0
    midnight = datetime.combine(entered.date() + timedelta(days=1), datetime.min.time())
    while midnight <= exited:
        ended = (midnight - timedelta(days=1)).weekday()
        if ended == triple_weekday:
            nights += 3
        elif ended < 5:
            nights += 1
        midnight += timedelta(days=1)
    return nights
