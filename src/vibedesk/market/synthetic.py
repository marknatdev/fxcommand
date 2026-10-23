"""Seeded, MT5-like synthetic bars for tests: weekends, gaps, missing minutes and spread spikes.

M1 bars are generated first. Every higher timeframe is aggregated from them on the server clock,
the same way MT5 builds its bars, so M1 always agrees exactly with M15 to D1.

By default the bars are a random walk with no edge. `SyntheticConfig` can plant a known edge in
entry timing (spec §9.6): each server day has a hidden direction that shows in the London opening
hour and drifts on for a few hours after it. A strategy that reads the opening hour can earn it;
a random entry at the same hours can't, because the direction is a coin flip it never sees.
"""

from __future__ import annotations

import math
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime

import numpy as np

from vibedesk.market.bars import Bars, Timeframe
from vibedesk.market.server_time import DAY, server_offset
from vibedesk.market.sessions import local_minute_of_day


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
    # The planted edge, in hourly volatilities. Zero for both means a pure random walk.
    reveal: float = 0.0  # drift over the London opening hour (08:00-09:00 London time)
    follow: float = 0.0  # drift per hour for `follow_hours` hours after the opening hour
    follow_hours: int = 0
    missing_bar_prob: float = 0.002
    spike_prob: float = 0.005
    weekend_gap_hours: float = 4.0  # weekend gap stdev, in hours of typical volatility


EDGE_BLOCK_DAYS = 20  # server days per block with as many hidden ups as downs

# The known-answer edge, calibrated in docs/research/2026-10-22-planted-edge.md on these seeds
# and this source range. Both matter: the base path and the day blocks start at the range start.
PLANTED_EDGE = SyntheticConfig(reveal=2.0, follow=0.6, follow_hours=3)
PLANTED_EDGE_SEEDS = (1, 2, 3, 4, 5)
PLANTED_EDGE_RANGE = (date(2014, 1, 1), date(2026, 10, 1))

# Volatility multiplier by server hour: quiet Asia, busy London and New York.
_INTRADAY = np.array([0.6] * 10 + [1.2] * 5 + [1.4] * 5 + [0.8] * 4)
_INTRADAY = _INTRADAY / _INTRADAY.mean()


class SyntheticBarSource:
    """A BarSource over a fixed date range; identical seeds give identical bars.

    tests/test_rules_version.py pins the bytes of bars made here (`inputs:bars`). A change that
    alters them needs a re-pin there, in a commit of its own, but no rules bump.
    """

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
        if cfg.reveal or cfg.follow:
            # A stream of its own, so the edge adds drift without moving any other draw.
            edge_rng = np.random.default_rng([self._seed, zlib.crc32(symbol.encode()), 1])
            direction = _hidden_directions(edge_rng, int(day_index.max()) + 1)
            returns += direction[day_index] * _edge_strength(minutes, cfg) * sigma / math.sqrt(60)

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


def _hidden_directions(rng: np.random.Generator, days: int) -> np.ndarray:
    """+1 or -1 per server day: a fair coin on each day, with ups and downs balanced per block.

    Independent coins would add a second random walk to the price level, and the Spread Floor
    is fixed in points, so a seed whose price the edge dragged down would pay several times
    the cost in R. Balancing each block of EDGE_BLOCK_DAYS days keeps the edge from moving the
    level, at the price of a slight anti-correlation between days of the same block.
    """
    blocks = -(-days // EDGE_BLOCK_DAYS)
    order = np.argsort(rng.random((blocks, EDGE_BLOCK_DAYS)), axis=1)
    return np.where(order < EDGE_BLOCK_DAYS // 2, 1.0, -1.0).ravel()[:days]


def _edge_strength(minutes: np.ndarray, cfg: SyntheticConfig) -> np.ndarray:
    """Drift per minute in minute volatilities, so each hour adds its strength in hourly ones.

    London time, not the server clock, places the opening hour, as the session Blocks do; the
    London open moves an hour against the server clock for a few weeks each spring and autumn.
    """
    since_open = local_minute_of_day(minutes, "london") - 8 * 60
    strength = np.zeros(len(minutes))
    strength[(since_open >= 0) & (since_open < 60)] = cfg.reveal
    strength[(since_open >= 60) & (since_open < 60 * (1 + cfg.follow_hours))] = cfg.follow
    return strength


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
