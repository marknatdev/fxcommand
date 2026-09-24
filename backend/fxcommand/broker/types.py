"""Value types that cross the Broker seam.

All timestamps are integer epoch seconds in *broker server time* (MT5 encodes
server wall-clock time as if it were UTC). Prices are in quote units.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Literal

import pandas as pd

Side = Literal["long", "short"]


class Timeframe(str, Enum):
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"

    @property
    def seconds(self) -> int:
        return TIMEFRAME_SECONDS[self]


TIMEFRAME_SECONDS = {
    Timeframe.M1: 60,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.M30: 1800,
    Timeframe.H1: 3600,
    Timeframe.H4: 14400,
    Timeframe.D1: 86400,
}

BAR_COLUMNS = ["time", "open", "high", "low", "close", "volume"]


def empty_bars() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in BAR_COLUMNS})


@dataclass(frozen=True)
class AccountInfo:
    login: int
    name: str
    server: str
    company: str
    currency: str
    balance: float
    equity: float
    margin: float
    margin_free: float
    leverage: int
    is_demo: bool
    trade_allowed: bool

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SymbolInfo:
    name: str
    description: str
    digits: int
    point: float
    trade_tick_size: float
    trade_tick_value: float  # account-currency value of one tick move for 1.0 lot
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int  # minimum SL/TP distance from price, in points
    filling_mode: int = 0  # MT5 SYMBOL_FILLING_* bitmask

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Tick:
    symbol: str
    time: int
    bid: float
    ask: float
    point: float

    @property
    def spread_points(self) -> float:
        return round((self.ask - self.bid) / self.point, 1)

    def to_dict(self) -> dict:
        return {**asdict(self), "spread_points": self.spread_points}


@dataclass(frozen=True)
class Position:
    ticket: int
    symbol: str
    side: Side
    volume: float
    price_open: float
    price_current: float
    sl: float
    tp: float
    profit: float
    magic: int
    time: int
    comment: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ClosedTrade:
    """A position that no longer exists on the Account, as reported by the Broker."""

    ticket: int  # the position id / ticket it had while open
    symbol: str
    side: Side
    volume: float
    price_open: float
    price_close: float
    profit: float  # includes swap and commission
    magic: int
    time_open: int
    time_close: int
    reason: str  # "sl" | "tp" | "manual" | "expert" | "other"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class OrderResult:
    ok: bool
    retcode: int
    message: str
    ticket: int | None = None
    price: float | None = None
    volume: float | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
