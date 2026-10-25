"""The seam between Vibe Desk and the MetaTrader 5 terminal: only the read-only calls M3 needs.

The real adapter is the `MetaTrader5` module itself, imported only here, inside `real_api()`,
which wraps it in ReadOnlyMt5. The fake is FakeMt5 (market/fake_mt5.py). tests/test_mt5_guard.py
fails on any MT5 function named off ALLOWED, as an attribute or a string, and on any reach into
the wrapped module, so an order call can't be written by accident (spec §3.3, §9.1). Python can't
make the wrapper airtight against one written on purpose; the scan is the guard.

MT5's quirks, which this contract keeps in view (probed read-only, 2026-10-23):
- Every time it returns (a bar's `time`, a tick's `time` and `time_msc`) is server wall-clock time
  written as if it were UTC. The XM server runs European time, EET/EEST (market/server_time.py).
- The range calls read their bounds the same way: a datetime whose UTC wall time is the server
  wall time wanted. A UTC 00:00 bound returns bars from server 00:00.
- A call that fails returns None; `last_error()` says why.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

import numpy as np

from vibedesk.market.bars import Timeframe

ALLOWED_CALLS = frozenset(
    {
        "initialize",
        "shutdown",
        "last_error",
        "symbol_info",
        "symbol_info_tick",
        "symbol_select",  # only adds a symbol to Market Watch, so its history can be read
        "copy_rates_range",
        "copy_ticks_range",
    }
)
ALLOWED_CONSTANTS = frozenset(
    {
        "TIMEFRAME_M1",
        "TIMEFRAME_M15",
        "TIMEFRAME_H1",
        "TIMEFRAME_H4",
        "TIMEFRAME_D1",
        "COPY_TICKS_ALL",
        "COPY_TICKS_INFO",
    }
)
ALLOWED = ALLOWED_CALLS | ALLOWED_CONSTANTS

# The MT5 constant for each Timeframe, by name, so callers look it up on whichever API they have.
TIMEFRAME_CONSTANTS = {
    Timeframe.M1: "TIMEFRAME_M1",
    Timeframe.M15: "TIMEFRAME_M15",
    Timeframe.H1: "TIMEFRAME_H1",
    Timeframe.H4: "TIMEFRAME_H4",
    Timeframe.D1: "TIMEFRAME_D1",
}

# What copy_rates_range and copy_ticks_range return (MetaTrader5 5.0.6231).
RATE_DTYPE = np.dtype(
    [
        ("time", "<i8"),
        ("open", "<f8"),
        ("high", "<f8"),
        ("low", "<f8"),
        ("close", "<f8"),
        ("tick_volume", "<u8"),
        ("spread", "<i4"),
        ("real_volume", "<u8"),
    ]
)
TICK_DTYPE = np.dtype(
    [
        ("time", "<i8"),
        ("bid", "<f8"),
        ("ask", "<f8"),
        ("last", "<f8"),
        ("volume", "<u8"),
        ("time_msc", "<i8"),
        ("flags", "<u4"),
        ("volume_real", "<f8"),
    ]
)

SWAP_IN_POINTS = 1  # symbol_info().swap_mode for swaps quoted in points per lot


class SymbolInfo(Protocol):
    """The fields of MT5's SymbolInfo that M3 reads."""

    name: str
    point: float
    digits: int
    swap_long: float  # points per lot per night; negative means the position pays
    swap_short: float
    swap_mode: int
    swap_rollover3days: int  # weekday of the triple swap, 0 = Sunday


class Tick(Protocol):
    """The fields of MT5's Tick that M3 reads."""

    time: int  # server wall-clock seconds
    time_msc: int  # the same, in milliseconds
    bid: float
    ask: float


class Mt5Api(Protocol):
    """The read-only MT5 calls and constants Vibe Desk may use, and nothing else."""

    TIMEFRAME_M1: int
    TIMEFRAME_M15: int
    TIMEFRAME_H1: int
    TIMEFRAME_H4: int
    TIMEFRAME_D1: int
    COPY_TICKS_ALL: int
    COPY_TICKS_INFO: int

    def initialize(self) -> bool: ...

    def shutdown(self) -> None: ...

    def last_error(self) -> tuple[int, str]: ...

    def symbol_info(self, symbol: str) -> SymbolInfo | None: ...

    def symbol_info_tick(self, symbol: str) -> Tick | None: ...

    def symbol_select(self, symbol: str, enable: bool = True) -> bool: ...

    def copy_rates_range(
        self, symbol: str, timeframe: int, date_from: datetime, date_to: datetime
    ) -> np.ndarray | None:
        """Bars (RATE_DTYPE) opening between the bounds, in server wall time. Whether an end
        exactly on a bound is included is not relied on: callers widen the range and trim."""
        ...

    def copy_ticks_range(
        self, symbol: str, date_from: datetime, date_to: datetime, flags: int
    ) -> np.ndarray | None:
        """Ticks (TICK_DTYPE) between the bounds, in server wall time."""
        ...


class ReadOnlyMt5:
    """The real MetaTrader5 module behind the allowlist: any other attribute raises, so code
    handed the real terminal can't call an order function by name. The module itself sits on
    `_module`, which only this file may touch (tests/test_mt5_guard.py)."""

    def __init__(self, module: object) -> None:
        object.__setattr__(self, "_module", module)

    def __getattr__(self, name: str):
        if name not in ALLOWED:
            raise AttributeError(f"MT5 {name!r} is not on Vibe Desk's read-only allowlist")
        return getattr(self._module, name)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("the MT5 API is read-only")


def real_api() -> Mt5Api:
    """The live terminal's API, behind the allowlist. Needs the `mt5` extra (Windows only)."""
    import MetaTrader5  # the only import of the real module in src/ (tests/test_mt5_guard.py)

    return ReadOnlyMt5(MetaTrader5)
