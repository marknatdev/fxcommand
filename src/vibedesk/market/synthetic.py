"""Seeded, MT5-like synthetic bars for tests: weekends, gaps, missing minutes and spread spikes.

M1 bars are generated first. Every higher timeframe is aggregated from them on the server clock,
the same way MT5 builds its bars, so M1 always agrees exactly with M15 to D1.
"""

from __future__ import annotations

import math
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime

import numpy as np

from vibedesk.market.bars import Bars, Timeframe
from vibedesk.market.server_time import DAY, server_offset


@dataclass(frozen=True)
class SymbolProfile:
    point: float
    digits: int
    start_price: float
    daily_vol: float  # typical daily return standard deviation, as a fraction of price
    spread_points: int  # typical minimum spread inside a bar
    spike_points: int  # spread during a spike


PROFILES = {
    "GOLD": SymbolProfile(0.01, 2, 1250.0, 0.010, 35, 150),
    "EURUSD": SymbolProfile(0.00001, 5, 1.30, 0.005, 15, 60),
    "GBPUSD": SymbolProfile(0.00001, 5, 1.55, 0.006, 18, 70),
    "USDJPY": SymbolProfile(0.001, 3, 105.0, 0.005, 18, 70),
    "AUDUSD": SymbolProfile(0.00001, 5, 0.90, 0.006, 20, 80),
    "USDCAD": SymbolProfile(0.00001, 5, 1.10, 0.005, 22, 80),
    "USDCHF": SymbolProfile(0.00001, 5, 0.95, 0.005, 22, 80),
    "NZDUSD": SymbolProfile(0.00001, 5, 0.80, 0.006, 25, 90),
}


@dataclass(frozen=True)
class SyntheticConfig:
    trend: float = 0.0  # planted edge: persistent drift, as a fraction of minute volatility
    trend_persistence_hours: float = 48.0
    missing_bar_prob: float = 0.002
    spike_prob: float = 0.005
    weekend_gap_hours: float = 4.0  # weekend gap stdev, in hours of typical volatility


# Volatility multiplier by server hour: quiet Asia, busy London and New York.
_INTRADAY = np.array([0.6] * 10 + [1.2] * 5 + [1.4] * 5 + [0.8] * 4)
_INTRADAY = _INTRADAY / _INTRADAY.mean()


class SyntheticBarSource:
    """A BarSource over a fixed date range; identical seeds give identical bars."""

    def __init__(
        self, seed: int, start: date, end: date, config: SyntheticConfig | None = None
    ) -> None:
        self._seed = seed
        self._start = int(datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp())
        self._end = int(datetime(end.year, end.month, end.day, tzinfo=UTC).timestamp())
        self._config = config or SyntheticConfig()
        self._cache: dict[tuple[str, Timeframe], Bars] = {}

    def fetch(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Bars:
        key = (symbol, timeframe)
        if key not in self._cache:
            m1 = self._m1(symbol)
            self._cache[key] = m1 if timeframe is Timeframe.M1 else _aggregate(m1, timeframe)
        bars = self._cache[key]
        lo = int(start.timestamp()) if start else bars.time[0] if len(bars) else 0
        hi = int(end.timestamp()) if end else self._end
        return bars.between(lo, hi)

    def _m1(self, symbol: str) -> Bars:
        key = (symbol, Timeframe.M1)
        if key not in self._cache:
            self._cache[key] = self._generate(symbol, PROFILES[symbol])
        return self._cache[key]

    def _generate(self, symbol: str, profile: SymbolProfile) -> Bars:
        cfg = self._config
        rng = np.random.default_rng([self._seed, zlib.crc32(symbol.encode())])

        minutes = np.arange(self._start, self._end, 60, dtype=np.int64)
        server = minutes + server_offset(minutes)
        # Server Monday to Friday only; 1970-01-01 was a Thursday.
        trading = ((server // DAY) + 3) % 7 < 5
        minutes, server = minutes[trading], server[trading]
        n = len(minutes)

        sigma_minute = profile.daily_vol / math.sqrt(1440)
        day_index = np.unique(server // DAY, return_inverse=True)[1]
        regime = _ar1(rng, int(day_index.max()) + 1, phi=0.97, sd=0.3)
        sigma = sigma_minute * np.exp(regime - 0.045)[day_index] * _INTRADAY[(server // 3600) % 24]

        returns = sigma * rng.standard_normal(n)
        if cfg.trend:
            hour_index = np.unique(server // 3600, return_inverse=True)[1]
            phi = math.exp(-1.0 / cfg.trend_persistence_hours)
            drift = _ar1(rng, int(hour_index.max()) + 1, phi=phi, sd=1.0)
            returns += cfg.trend * sigma * drift[hour_index]

        gaps = np.zeros(n)
        week_opens = np.flatnonzero(np.diff(minutes) > DAY) + 1
        gap_sd = sigma_minute * math.sqrt(60) * cfg.weekend_gap_hours
        gaps[week_opens] = gap_sd * rng.standard_normal(len(week_opens))

        log_close = math.log(profile.start_price) + np.cumsum(gaps + returns)
        close = np.exp(log_close)
        open_ = np.exp(log_close - returns)
        high = np.maximum(open_, close) * np.exp(np.abs(rng.standard_normal(n)) * sigma * 0.6)
        low = np.minimum(open_, close) * np.exp(-np.abs(rng.standard_normal(n)) * sigma * 0.6)

        spread = profile.spread_points + rng.integers(0, profile.spread_points // 3 + 1, n)
        spread[rng.random(n) < cfg.spike_prob] = profile.spike_points

        keep = rng.random(n) >= cfg.missing_bar_prob
        point, digits = profile.point, profile.digits
        open_, close = _on_grid(open_, point, digits), _on_grid(close, point, digits)
        high = np.maximum(_on_grid(high, point, digits), np.maximum(open_, close))
        low = np.minimum(_on_grid(low, point, digits), np.minimum(open_, close))
        return Bars(
            symbol=symbol,
            timeframe=Timeframe.M1,
            point=point,
            digits=digits,
            time=minutes[keep],
            open=open_[keep],
            high=high[keep],
            low=low[keep],
            close=close[keep],
            spread=spread[keep],
        )


def _ar1(rng: np.random.Generator, n: int, phi: float, sd: float) -> np.ndarray:
    """A stationary AR(1) series with standard deviation `sd`."""
    shocks = rng.standard_normal(n) * sd * math.sqrt(1 - phi * phi)
    series = np.empty(n)
    level = rng.standard_normal() * sd
    for i in range(n):
        level = phi * level + shocks[i]
        series[i] = level
    return series


def _on_grid(prices: np.ndarray, point: float, digits: int) -> np.ndarray:
    return np.round(np.round(prices / point) * point, digits)


def _aggregate(m1: Bars, timeframe: Timeframe) -> Bars:
    """Build higher-timeframe bars from M1 on server-clock boundaries, as MT5 does."""
    offset = server_offset(m1.time)
    bucket = (m1.time + offset) // timeframe.seconds
    starts = np.flatnonzero(np.r_[True, bucket[1:] != bucket[:-1]])
    ends = np.r_[starts[1:], len(m1)] - 1
    return Bars(
        symbol=m1.symbol,
        timeframe=timeframe,
        point=m1.point,
        digits=m1.digits,
        time=bucket[starts] * timeframe.seconds - offset[starts],
        open=m1.open[starts],
        high=np.maximum.reduceat(m1.high, starts),
        low=np.minimum.reduceat(m1.low, starts),
        close=m1.close[ends],
        spread=np.minimum.reduceat(m1.spread, starts),
    )
