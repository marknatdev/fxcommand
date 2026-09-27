"""Trading costs for Backtests, Shadow Trades, the Evidence Run and the Paper book.

A ``CostModel`` is built from a symbol's live specification: spread, slippage and overnight swap.
Multi-year backtests scale those costs with price (spec D34). Today's $0.57 GOLD spread is a
much larger share of a bar when GOLD traded at $1,200, and charging it unscaled overstates the
cost of the early years 2.5–3.5×. Swap is charged once per server-day rollover, and three
times on the symbol's triple-swap day, the way MetaTrader 5 books it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

DAY = 86400
COST_MODEL_VERSION = 1  # part of every Evidence key: bump when the cost rules change

# MetaTrader 5 ENUM_SYMBOL_SWAP_MODE
SWAP_DISABLED = 0
SWAP_POINTS = 1
SWAP_CURRENCY_SYMBOL = 2
SWAP_CURRENCY_MARGIN = 3
SWAP_CURRENCY_DEPOSIT = 4
SWAP_INTEREST_CURRENT = 5
SWAP_INTEREST_OPEN = 6


def _weekday(day: int) -> int:
    """0 = Monday … 6 = Sunday for an epoch day number (1970-01-01 was a Thursday)."""
    return (day + 3) % 7


def mt5_day_to_weekday(mt5_day: int) -> int:
    """MT5 ENUM_DAY_OF_WEEK (0 = Sunday … 6 = Saturday) to 0 = Monday … 6 = Sunday."""
    return (mt5_day - 1) % 7


def rollover_nights(open_ts: int, close_ts: int, triple_weekday: int = 2) -> int:
    """Swap nights charged between opening and closing: one per server midnight that ends a trading
    day (Monday–Friday), three for the midnight that ends ``triple_weekday`` (0 = Monday; Wednesday
    by default), which is how the weekend is paid for."""
    if close_ts <= open_ts:
        return 0
    nights = 0
    for day in range(open_ts // DAY, close_ts // DAY):  # midnight at (day + 1) * DAY lies in (open, close]
        wd = _weekday(day)
        if wd <= 4:
            nights += 3 if wd == triple_weekday else 1
    return nights


@dataclass(frozen=True)
class CostModel:
    spread: float  # price units (ask − bid), today's typical value
    slippage: float = 0.0  # price units per market or stop fill
    swap_long: float = 0.0  # as the symbol reports it, per lot per night (sign: + earns, − pays)
    swap_short: float = 0.0
    swap_mode: int = SWAP_DISABLED
    triple_weekday: int = 2  # 0 = Monday; MT5 reports it as swap_rollover3days (see mt5_day_to_weekday)
    point: float = 0.01
    value_per_price: float = 1.0  # account currency per 1.0 price move per lot (tick_value / tick_size)
    ref_price: float | None = None  # today's price; None switches price scaling off
    version: int = COST_MODEL_VERSION

    def scale(self, price: float) -> float:
        return price / self.ref_price if self.ref_price and price > 0 else 1.0

    def spread_at(self, price: float) -> float:
        return self.spread * self.scale(price)

    def slippage_at(self, price: float) -> float:
        return self.slippage * self.scale(price)

    def swap_per_night(self, side: str, price: float) -> float:
        """Signed swap for one night in price units (negative = a cost), for a position at ``price``."""
        raw = self.swap_long if side == "long" else self.swap_short
        if raw == 0 or self.swap_mode == SWAP_DISABLED:
            return 0.0
        if self.swap_mode == SWAP_POINTS:
            return raw * self.point * self.scale(price)
        if self.swap_mode in (SWAP_CURRENCY_SYMBOL, SWAP_CURRENCY_MARGIN, SWAP_CURRENCY_DEPOSIT):
            # money per lot per night; converted as deposit currency (exact for USD accounts on USD-quoted symbols)
            return raw / self.value_per_price * self.scale(price) if self.value_per_price else 0.0
        if self.swap_mode in (SWAP_INTEREST_CURRENT, SWAP_INTEREST_OPEN):
            return raw / 100.0 / 360.0 * price  # annual % of price, already proportional to price
        return 0.0  # reopen modes are not modelled

    def swap_for(self, side: str, entry_price: float, open_ts: int, close_ts: int) -> float:
        """Signed swap in price units for a position held from ``open_ts`` to ``close_ts``."""
        nights = rollover_nights(open_ts, close_ts, self.triple_weekday)
        return self.swap_per_night(side, entry_price) * nights if nights else 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_symbol(cls, info, spread: float, slippage_points: float = 1.0, ref_price: float | None = None) -> "CostModel":
        """Build from a broker ``SymbolInfo`` and a typical spread in price units (from ticks or the bar
        median, never one tick at the daily break). Swap fields are optional until the broker reports them."""
        point = float(info.point)
        tick_size = float(getattr(info, "trade_tick_size", 0) or point)
        tick_value = float(getattr(info, "trade_tick_value", 0) or 0)
        return cls(
            spread=float(spread),
            slippage=slippage_points * point,
            swap_long=float(getattr(info, "swap_long", 0) or 0),
            swap_short=float(getattr(info, "swap_short", 0) or 0),
            swap_mode=int(getattr(info, "swap_mode", SWAP_DISABLED) or SWAP_DISABLED),
            triple_weekday=mt5_day_to_weekday(int(getattr(info, "swap_rollover3days", 3) or 3)),
            point=point,
            value_per_price=tick_value / tick_size if tick_size else 0.0,
            ref_price=ref_price,
        )
