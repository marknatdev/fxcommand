"""Risk Gate: every Signal passes here before it becomes an order.

Pure functions: no Broker, no database. ``check`` turns an entry intent into a
sized order or a rejection with a human-readable reason; ``manage`` proposes
stop-loss moves (breakeven / ATR trailing) for an open position.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from ..broker.types import AccountInfo, Position, Side, SymbolInfo, Tick
from .window import TradingWindow


MAX_QUOTE_AGE = 120  # seconds of server time; older quotes are not traded on


@dataclass(frozen=True)
class RiskProfile:
    name: str = "Default"
    risk_pct: float = 1.0  # % of equity lost if the stop-loss is hit
    max_spread_points: float = 30.0
    breakeven: bool = False
    breakeven_at_r: float = 1.0  # move SL to entry once profit reaches this many R
    trailing: bool = False
    trailing_atr: float = 2.0  # trail SL this many ATR behind price
    trailing_start_r: float = 1.0  # ...once profit reaches this many R

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RiskLimits:
    max_positions_session: int = 5
    max_positions_global: int = 5
    daily_loss_pct_session: float = 3.0
    daily_loss_pct_global: float = 3.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Exposure:
    """What is already at stake when the intent arrives. P&L = today's realised + floating, server day."""

    session_positions: int
    global_positions: int
    session_day_pnl: float
    global_day_pnl: float
    day_start_equity: float


@dataclass(frozen=True)
class GateInput:
    symbol: SymbolInfo
    tick: Tick
    side: Side
    sl_dist: float
    tp_dist: float
    account: AccountInfo
    live_enabled: bool
    profile: RiskProfile
    session_limits: RiskLimits
    global_limits: RiskLimits
    exposure: Exposure
    window: TradingWindow
    now: int


@dataclass(frozen=True)
class Approved:
    volume: float
    entry: float
    sl: float
    tp: float
    risk_amount: float  # account currency lost if SL is hit at this volume
    ok: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Rejected:
    code: str
    reason: str
    ok: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


Decision = Approved | Rejected


def _decimals(step: float) -> int:
    return max(0, -int(math.floor(math.log10(step) + 1e-9))) if step < 1 else 0


def size_volume(risk_amount: float, sl_dist: float, info: SymbolInfo) -> float:
    """Lots whose stop-loss loss is at most ``risk_amount``. Rounded DOWN to volume_step; 0 if below minimum."""
    if sl_dist <= 0 or info.trade_tick_size <= 0 or info.trade_tick_value <= 0:
        return 0.0
    loss_per_lot = sl_dist / info.trade_tick_size * info.trade_tick_value
    raw = risk_amount / loss_per_lot
    steps = math.floor(raw / info.volume_step + 1e-9)
    vol = round(steps * info.volume_step, _decimals(info.volume_step))
    vol = min(vol, info.volume_max)
    return vol if vol >= info.volume_min - 1e-12 else 0.0


def loss_at_stop(volume: float, sl_dist: float, info: SymbolInfo) -> float:
    return volume * sl_dist / info.trade_tick_size * info.trade_tick_value


def check(g: GateInput) -> Decision:
    info, tick, acct = g.symbol, g.tick, g.account

    if not acct.is_demo and not g.live_enabled:
        return Rejected("live_blocked", f"account {acct.login} is LIVE and not live-enabled")
    if not acct.trade_allowed:
        return Rejected("trade_disabled", "trading is disabled in the terminal (AutoTrading off or account read-only)")
    if g.now - tick.time > MAX_QUOTE_AGE:
        return Rejected("stale_quote", f"last {info.name} quote is {g.now - tick.time}s old (max {MAX_QUOTE_AGE}s); market closed or feed stalled")
    if not g.window.is_open(g.now):
        return Rejected("outside_window", f"outside trading window ({g.window.describe()})")
    if tick.spread_points > g.profile.max_spread_points:
        return Rejected("spread", f"spread {tick.spread_points} pts > max {g.profile.max_spread_points} pts")

    ex = g.exposure
    if ex.session_positions >= g.session_limits.max_positions_session:
        return Rejected("max_positions_session", f"session already has {ex.session_positions} open positions (max {g.session_limits.max_positions_session})")
    if ex.global_positions >= g.global_limits.max_positions_global:
        return Rejected("max_positions_global", f"{ex.global_positions} open positions across all sessions (max {g.global_limits.max_positions_global})")
    s_cap = -g.session_limits.daily_loss_pct_session / 100 * ex.day_start_equity
    if ex.session_day_pnl <= s_cap:
        return Rejected("daily_loss_session", f"session daily P&L {ex.session_day_pnl:.2f} hit limit {s_cap:.2f}")
    g_cap = -g.global_limits.daily_loss_pct_global / 100 * ex.day_start_equity
    if ex.global_day_pnl <= g_cap:
        return Rejected("daily_loss_global", f"global daily P&L {ex.global_day_pnl:.2f} hit limit {g_cap:.2f}")

    if g.sl_dist <= 0:
        return Rejected("no_stop", "signal has no stop-loss distance; every order must carry a stop-loss")

    d = info.digits
    min_stop = info.stops_level * info.point
    spread = tick.ask - tick.bid
    if g.side == "long":
        entry = tick.ask
        sl = round(entry - g.sl_dist, d)
        tp = round(entry + g.tp_dist, d) if g.tp_dist > 0 else 0.0
        sl_gap, tp_gap = tick.bid - sl, (tp - tick.bid) if tp else None
    else:
        entry = tick.bid
        sl = round(entry + g.sl_dist, d)
        tp = round(entry - g.tp_dist, d) if g.tp_dist > 0 else 0.0
        sl_gap, tp_gap = sl - tick.ask, (tick.ask - tp) if tp else None
    if sl_gap < min_stop - 1e-12:
        return Rejected(
            "stops_level",
            f"stop-loss {abs(entry - sl) / info.point:.0f} pts from entry is inside the broker minimum "
            f"({info.stops_level} pts + {spread / info.point:.0f} pts spread)",
        )
    if tp_gap is not None and tp_gap < min_stop - 1e-12:
        return Rejected("stops_level", f"take-profit is inside the broker minimum stop distance ({info.stops_level} pts)")

    risk_amount = acct.equity * g.profile.risk_pct / 100
    actual_sl_dist = abs(entry - sl)
    volume = size_volume(risk_amount, actual_sl_dist, info)
    if volume <= 0:
        need = loss_at_stop(info.volume_min, actual_sl_dist, info)
        return Rejected(
            "volume_min",
            f"risk {risk_amount:.2f} {acct.currency} buys less than the minimum {info.volume_min} lots "
            f"(minimum lot would risk {need:.2f})",
        )
    return Approved(volume=volume, entry=entry, sl=sl, tp=tp, risk_amount=round(loss_at_stop(volume, actual_sl_dist, info), 2))


def manage(
    position: Position, tick: Tick, info: SymbolInfo, atr: float, initial_risk: float, profile: RiskProfile
) -> float | None:
    """New stop-loss for breakeven / trailing, or None. Only ever tightens; respects the broker stop level."""
    if not (profile.breakeven or profile.trailing) or initial_risk <= 0:
        return None
    long = position.side == "long"
    price = tick.bid if long else tick.ask
    profit_dist = (price - position.price_open) if long else (position.price_open - price)
    candidates: list[float] = []
    if profile.breakeven and profit_dist >= profile.breakeven_at_r * initial_risk:
        candidates.append(position.price_open)
    if profile.trailing and atr > 0 and profit_dist >= profile.trailing_start_r * initial_risk:
        candidates.append(price - profile.trailing_atr * atr if long else price + profile.trailing_atr * atr)
    if not candidates:
        return None
    new_sl = round(max(candidates) if long else min(candidates), info.digits)
    cur = position.sl
    min_stop = info.stops_level * info.point
    if long:
        improves = cur == 0 or new_sl > cur + info.point / 2
        legal = tick.bid - new_sl >= min_stop
        # the modify request re-sends the TP; the broker rejects it once price is inside the TP's stop level
        tp_ok = not position.tp or position.tp - tick.bid >= min_stop
    else:
        improves = cur == 0 or new_sl < cur - info.point / 2
        legal = new_sl - tick.ask >= min_stop
        tp_ok = not position.tp or tick.ask - position.tp >= min_stop
    return new_sl if improves and legal and tp_ok else None
