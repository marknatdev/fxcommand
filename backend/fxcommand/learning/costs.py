"""Trading costs for Backtests, Shadow Trades, the Evidence Run and the Paper book.

A ``CostModel`` is built from a symbol's live specification: spread, slippage and overnight swap.
Multi-year backtests scale those costs with price (spec D34). Today's $0.57 GOLD spread is a
much larger share of a bar when GOLD traded at $1,200, and charging it unscaled overstates the
cost of the early years 2.5–3.5×. Swap is charged once per server-day rollover, and three
times on the symbol's triple-swap day, the way MetaTrader 5 books it. A symbol that reports no
valid triple day (BTCUSD on XM reports 7) trades every day and is charged every night.

A ``SymbolCostProfile`` holds what a symbol's pricing needs beyond its specification: where its
Trusted History starts, and whether each bar is priced at no less than its recorded spread
(spec-btc-strategies D5, D14, D15).
"""

from __future__ import annotations

import calendar
from dataclasses import asdict, dataclass

DAY = 86400
COST_MODEL_VERSION = 2  # part of every Evidence key: bump when the cost rules change (2: every-night swap, bar floor)

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


def rollover_nights(open_ts: int, close_ts: int, triple_weekday: int = 2, every_night: bool = False) -> int:
    """Swap nights charged between opening and closing: one per server midnight that ends a trading
    day (Monday–Friday), three for the midnight that ends ``triple_weekday`` (0 = Monday; Wednesday
    by default), which is how the weekend is paid for. With ``every_night`` (a symbol that trades
    every day), one per server midnight, weekends included, and no triple day."""
    if close_ts <= open_ts:
        return 0
    if every_night:
        return close_ts // DAY - open_ts // DAY
    nights = 0
    for day in range(open_ts // DAY, close_ts // DAY):  # midnight at (day + 1) * DAY lies in (open, close]
        wd = _weekday(day)
        if wd <= 4:
            nights += 3 if wd == triple_weekday else 1
    return nights


@dataclass(frozen=True)
class SymbolCostProfile:
    history_from: int | None = None  # Trusted History start (server time); None = research.RESEARCH_START
    bar_spread_floor: bool = False  # price each bar at no less than the spread it recorded


DEFAULT_PROFILE = SymbolCostProfile()
# Keyed by the broker's symbol name. Pinned by tests: the Strategy Review may not change it.
SYMBOL_PROFILES: dict[str, SymbolCostProfile] = {
    # 2013–2015 intraday bars are daily bars copied down; 2019–21 spreads were 3–10x today's, scaled
    "BTCUSD": SymbolCostProfile(history_from=calendar.timegm((2018, 1, 1, 0, 0, 0)), bar_spread_floor=True),
}


def profile(symbol: str) -> SymbolCostProfile:
    return SYMBOL_PROFILES.get(symbol, DEFAULT_PROFILE)


# Research and Evidence judge trades signalled from here on (the spec's periods 2010–17, 2018–21,
# 2022–26); older history only warms indicators up (GOLD spec D56)
RESEARCH_START = calendar.timegm((2010, 1, 1, 0, 0, 0))


def trusted_from(symbol: str) -> int:
    """Where a symbol's Trusted History starts: the one definition (spec-btc-strategies D15)."""
    return max(RESEARCH_START, profile(symbol).history_from or RESEARCH_START)


def swap_every_night(mt5_day: int) -> bool:
    """A triple-swap day outside MT5's 0–6 means the symbol is charged every night (BTCUSD reports 7)."""
    return not 0 <= int(mt5_day) <= 6


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
    swap_every_night: bool = False  # 7 nights a week, no triple day (a symbol that trades every day)
    bar_floor: float = 0.0  # x the bar's recorded spread as the least spread charged; 0 = off
    version: int = COST_MODEL_VERSION

    def scale(self, price: float) -> float:
        return price / self.ref_price if self.ref_price and price > 0 else 1.0

    def spread_at(self, price: float, recorded: float = 0.0) -> float:
        """The spread charged at ``price``; ``recorded`` is the bar's own spread (price units), a floor
        when ``bar_floor`` is on. A bar that recorded none falls back to price scaling."""
        scaled = self.spread * self.scale(price)
        return max(scaled, recorded * self.bar_floor) if self.bar_floor and recorded > 0 else scaled

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
        nights = self.nights(open_ts, close_ts)
        return self.swap_per_night(side, entry_price) * nights if nights else 0.0

    def nights(self, open_ts: int, close_ts: int) -> int:
        return rollover_nights(open_ts, close_ts, self.triple_weekday, self.swap_every_night)

    @property
    def swap_nights_key(self) -> str:
        """How nights are counted, for Evidence keys: ``n7`` every night, else ``n5t<triple weekday>``."""
        return "n7" if self.swap_every_night else f"n5t{self.triple_weekday}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_symbol(
        cls, info, spread: float, slippage_points: float = 1.0, ref_price: float | None = None, floor_mult: float = 1.0
    ) -> "CostModel":
        """Build from a broker ``SymbolInfo`` and a typical spread in price units (from ticks or the bar
        median, never one tick at the daily break). Swap fields are optional until the broker reports them.
        ``floor_mult`` multiplies the recorded bar spread when the symbol's profile asks for a floor
        (Evidence and research pass their spread multiplier, so both prices are doubled alike)."""
        point = float(info.point)
        rollover = int(getattr(info, "swap_rollover3days", 3))
        every = swap_every_night(rollover)
        tick_size = float(getattr(info, "trade_tick_size", 0) or point)
        tick_value = float(getattr(info, "trade_tick_value", 0) or 0)
        return cls(
            spread=float(spread),
            slippage=slippage_points * point,
            swap_long=float(getattr(info, "swap_long", 0) or 0),
            swap_short=float(getattr(info, "swap_short", 0) or 0),
            swap_mode=int(getattr(info, "swap_mode", SWAP_DISABLED) or SWAP_DISABLED),
            triple_weekday=2 if every else mt5_day_to_weekday(rollover or 3),
            point=point,
            value_per_price=tick_value / tick_size if tick_size else 0.0,
            ref_price=ref_price,
            swap_every_night=every,
            bar_floor=float(floor_mult) if profile(str(getattr(info, "name", ""))).bar_spread_floor else 0.0,
        )
