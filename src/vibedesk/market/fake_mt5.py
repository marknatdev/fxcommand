"""FakeMt5: the MT5 terminal as the tests need it, behind the same contract as the real one.

It serves a BarSource's bars (a SyntheticBarSource, in tests) the way MT5 does: times in server
wall-clock seconds written as UTC, bounds read the same way, None plus `last_error()` on
failure. It can also misbehave the ways a real terminal does, so the importer can be tested
against them:
- `settling_reads`: the first reads of each range return only part of it, as while the terminal
  is still downloading history;
- `max_bars`: a read returns at most this many bars (the latest), as with a low "Max bars in
  chart" setting;
- `now`: a clock. Bars that open after it don't exist yet, and the bar open at `now` is still
  forming. A callable clock can move between two reads, as a live market does.
Ticks follow the M1 bars: `ticks_per_minute` per bar, with spreads cycling through
`tick_spreads` (points), and every `missing_side_every`-th tick missing its ask.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from vibedesk.market.bars import Bars, Timeframe
from vibedesk.market.mt5_api import RATE_DTYPE, SWAP_IN_POINTS, TICK_DTYPE
from vibedesk.market.server_time import server_offset
from vibedesk.market.source import BarSource
from vibedesk.market.synthetic import PROFILES

# MT5's error codes for the failures the fake can produce.
RES_S_OK = (1, "Success")
NO_IPC = (-10004, "No IPC connection")
INVALID_PARAMS = (-2, "Invalid params")
INIT_FAILED = (-10003, "IPC initialize failed")

_TIMEFRAMES = {1: Timeframe.M1, 15: Timeframe.M15, 16385: Timeframe.H1, 16388: Timeframe.H4,
               16408: Timeframe.D1}  # fmt: skip


@dataclass(frozen=True, slots=True)
class FakeSymbolInfo:
    name: str
    point: float
    digits: int
    swap_long: float
    swap_short: float
    swap_mode: int = SWAP_IN_POINTS
    swap_rollover3days: int = 3  # Wednesday


@dataclass(frozen=True, slots=True)
class FakeTick:
    time: int
    time_msc: int
    bid: float
    ask: float


class FakeMt5:
    """A stand-in for the MetaTrader5 module (see the module docstring)."""

    # The real module's values (MetaTrader5 5.0.6231).
    TIMEFRAME_M1 = 1
    TIMEFRAME_M15 = 15
    TIMEFRAME_H1 = 16385
    TIMEFRAME_H4 = 16388
    TIMEFRAME_D1 = 16408
    COPY_TICKS_ALL = -1
    COPY_TICKS_INFO = 1

    def __init__(
        self,
        source: BarSource,  # point and digits come from the synthetic PROFILES
        *,
        now: datetime | Callable[[], datetime] | None = None,
        swaps: dict[str, tuple[float, float]] | None = None,
        swap_mode: int = SWAP_IN_POINTS,
        tick_spreads: Sequence[int] = (50, 55, 55, 60),
        ticks_per_minute: int = 2,
        missing_side_every: int = 0,
        settling_reads: int = 0,
        max_bars: int | None = None,
        fail_initialize: bool = False,
    ) -> None:
        self._source = source
        self._now = now
        self._swaps = swaps or {}
        self._swap_mode = swap_mode
        self._tick_spreads = np.asarray(tick_spreads, dtype=np.int64)
        self._ticks_per_minute = ticks_per_minute
        self._missing_side_every = missing_side_every
        self._settling_reads = settling_reads
        self._max_bars = max_bars
        self._fail_initialize = fail_initialize
        self._initialized = False
        self._error = RES_S_OK
        self._reads: dict[tuple, int] = {}
        self._server: dict[tuple[str, Timeframe], tuple[Bars, np.ndarray]] = {}
        self.selected: set[str] = set()
        self.calls: list[tuple] = []  # every call, with its arguments, for tests to inspect

    # Connection

    def initialize(self) -> bool:
        self.calls.append(("initialize",))
        if self._fail_initialize:
            self._error = INIT_FAILED
            return False
        self._initialized, self._error = True, RES_S_OK
        return True

    def shutdown(self) -> None:
        self.calls.append(("shutdown",))
        self._initialized = False

    def last_error(self) -> tuple[int, str]:
        return self._error

    # Symbols

    def symbol_info(self, symbol: str) -> FakeSymbolInfo | None:
        self.calls.append(("symbol_info", symbol))
        if not self._ready() or not self._known(symbol):
            return None
        profile = PROFILES[symbol]
        swap_long, swap_short = self._swaps.get(symbol, (-10.0, 2.0))
        return FakeSymbolInfo(
            symbol, profile.point, profile.digits, swap_long, swap_short, self._swap_mode
        )

    def symbol_select(self, symbol: str, enable: bool = True) -> bool:
        self.calls.append(("symbol_select", symbol, enable))
        if not self._ready() or not self._known(symbol):
            return False
        (self.selected.add if enable else self.selected.discard)(symbol)
        return True

    def symbol_info_tick(self, symbol: str) -> FakeTick | None:
        """The latest tick: at `now` while the market is open, else the last minute's close."""
        self.calls.append(("symbol_info_tick", symbol))
        if not self._ready() or not self._known(symbol):
            return None
        m1, server = self._series(symbol, Timeframe.M1)
        if not len(m1):
            return None
        last = len(m1) - 1
        utc = int(m1.time[last]) + 59
        now = self._utc_now()
        if now is not None:
            utc = min(now, utc)
        time = utc + int(server[last] - m1.time[last])
        bid = float(m1.close[last])
        ask = bid + int(self._tick_spreads[0]) * PROFILES[symbol].point
        return FakeTick(time, time * 1000, bid, ask)

    # History

    def copy_rates_range(
        self, symbol: str, timeframe: int, date_from: datetime, date_to: datetime
    ) -> np.ndarray | None:
        self.calls.append(("copy_rates_range", symbol, timeframe, date_from, date_to))
        if not self._ready():
            return None
        if not self._known(symbol) or timeframe not in _TIMEFRAMES:
            self._error = INVALID_PARAMS
            return None
        lo, hi = _wall_seconds(date_from), _wall_seconds(date_to)
        bars, server = self._series(symbol, _TIMEFRAMES[timeframe])
        a, b = np.searchsorted(server, [lo, hi + 1])
        out = np.zeros(b - a, dtype=RATE_DTYPE)
        out["time"] = server[a:b]
        for name in ("open", "high", "low", "close", "spread"):
            out[name] = getattr(bars, name)[a:b]
        out["tick_volume"] = 1
        if self._max_bars is not None:
            out = out[-self._max_bars :]
        return self._settle(("rates", symbol, timeframe, lo, hi), out)

    def copy_ticks_range(
        self, symbol: str, date_from: datetime, date_to: datetime, flags: int
    ) -> np.ndarray | None:
        self.calls.append(("copy_ticks_range", symbol, date_from, date_to, flags))
        if not self._ready():
            return None
        if not self._known(symbol):
            self._error = INVALID_PARAMS
            return None
        lo, hi = _wall_seconds(date_from), _wall_seconds(date_to)
        m1, server = self._series(symbol, Timeframe.M1)
        a, b = np.searchsorted(server, [lo - 59, hi + 1])
        per = self._ticks_per_minute
        times = (server[a:b, None] + (60 // per) * np.arange(per)[None, :]).ravel()
        bids = np.repeat(m1.close[a:b], per)
        keep = (times >= lo) & (times <= hi)
        times, bids = times[keep], bids[keep]
        out = np.zeros(len(times), dtype=TICK_DTYPE)
        out["time"] = times
        out["time_msc"] = times * 1000
        out["bid"] = bids
        spreads = self._tick_spreads[np.arange(len(times)) % len(self._tick_spreads)]
        out["ask"] = bids + spreads * PROFILES[symbol].point
        if self._missing_side_every:
            out["ask"][self._missing_side_every - 1 :: self._missing_side_every] = 0.0
        out["flags"] = 6  # TICK_FLAG_BID | TICK_FLAG_ASK
        return out

    # Internals

    def _ready(self) -> bool:
        if not self._initialized:
            self._error = NO_IPC
        return self._initialized

    def _known(self, symbol: str) -> bool:
        if symbol not in PROFILES:
            self._error = INVALID_PARAMS
            return False
        return True

    def _utc_now(self) -> int | None:
        now = self._now() if callable(self._now) else self._now
        return None if now is None else int(now.timestamp())

    def _series(self, symbol: str, timeframe: Timeframe) -> tuple[Bars, np.ndarray]:
        """The source's bars that exist at `now`, and their server wall times. A bar exists once
        it has opened; the last one may still be forming."""
        key = (symbol, timeframe)
        if key not in self._server:
            bars = self._source.fetch(symbol, timeframe)
            self._server[key] = (bars, bars.time + server_offset(bars.time))
        bars, server = self._server[key]
        now = self._utc_now()
        if now is None:
            return bars, server
        n = int(np.searchsorted(bars.time, now, "right"))  # bars opened at or before now
        return bars.between(-(2**62), now + 1), server[:n]

    def _settle(self, key: tuple, out: np.ndarray) -> np.ndarray:
        """While the terminal 'downloads', a read returns only the first half of the range."""
        reads = self._reads.get(key, 0)
        self._reads[key] = reads + 1
        return out[: len(out) // 2] if reads < self._settling_reads else out


def _wall_seconds(bound: datetime) -> int:
    """MT5 reads a bound's UTC wall time as server wall time; a naive datetime would be read in
    the computer's own zone, so the fake refuses it."""
    if bound.tzinfo is None or bound.utcoffset() != UTC.utcoffset(None):
        raise ValueError("MT5 bounds are server wall time written as UTC: pass a UTC datetime")
    return int(bound.timestamp())
