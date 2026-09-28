"""SimBroker: a deterministic, seeded, in-memory market.

Time moves only when ``step()`` is called (the app drives it from a clock task
in real time, tests drive it by hand). Each step produces one M1 bar per
Symbol, then checks every open position's stop-loss / take-profit against that
bar. Weekends are skipped, like a real FX market. Bars are bid prices; ask is
bid + spread.

A Symbol may have daily trading hours (GOLD is shut 23:57-01:00 server time every weekday, like
XM). While it is shut no bars are produced, orders are refused as market closed and the price
keeps moving underneath, so the reopen gaps. The first minutes after the reopen quote a wider
spread.

``deep_history_days`` adds older history at H1 resolution before the M1 history, so the long
Timeframes (H1, H4, D1) reach back far enough for evidence runs without holding M1 bars for years.

Contract specifications mimic an XM USD account so position sizing behaves as
it would against the real terminal (FX majors, a JPY pair, and GOLD with a
100 oz contract).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .base import BrokerError
from .types import (
    AccountInfo,
    ClosedTrade,
    OrderResult,
    Position,
    Side,
    SymbolInfo,
    TerminalStatus,
    Tick,
    Timeframe,
    empty_bars,
)

RET_REQUOTE = 10004
RET_DONE = 10009
RET_DONE_PARTIAL = 10010
RET_TIMEOUT = 10012
RET_PRICE_OFF = 10021
RET_MARKET_CLOSED = 10018
RET_INVALID = 10013
RET_INVALID_VOLUME = 10014
RET_INVALID_STOPS = 10016
RET_NO_MONEY = 10019
RET_POSITION_NOT_FOUND = 10036

# Fault injection (tests and /api/sim/fault): each queued fault applies to the next matching call.
ENTRY_FAULTS = ("requote", "timeout_filled", "timeout_none", "partial", "price_zero", "hang")
CLOSE_FAULTS = ("close_fail", "close_timeout_done")

MINUTE = 60
DAY = 86400


@dataclass(frozen=True)
class SimSymbolSpec:
    name: str
    description: str
    base: str
    quote: str
    price: float
    digits: int
    contract_size: float
    sigma: float  # per-minute relative volatility
    spread_points: int
    stops_level: int = 10
    volume_min: float = 0.01
    volume_max: float = 50.0
    volume_step: float = 0.01
    open_minutes: tuple[int, int] | None = None  # Mon-Fri [start, end) minute of day; None = all day
    reopen_spread_points: int | None = None  # spread just after the daily reopen
    reopen_minutes: int = 15
    swap_long: float = 0.0  # MT5 swap fields; points per lot per night (swap_mode 1)
    swap_short: float = 0.0
    swap_mode: int = 1
    swap_rollover3days: int = 3  # MT5 day of week (0 = Sunday): Wednesday charges three nights
    stream: int = 0  # 0 = the shared generator; >0 = its own, so adding the Symbol moves no other Symbol's path

    @property
    def point(self) -> float:
        return 10.0 ** -self.digits


# The market before trading hours and swaps were modelled. The golden fixtures are generated on it so
# a change to the default specs never moves their pinned inputs.
LEGACY_SYMBOLS: tuple[SimSymbolSpec, ...] = (
    SimSymbolSpec("EURUSD", "Euro vs US Dollar", "EUR", "USD", 1.0850, 5, 100_000, 7e-5, 12),
    SimSymbolSpec("GBPUSD", "Great Britain Pound vs US Dollar", "GBP", "USD", 1.2700, 5, 100_000, 8e-5, 16),
    SimSymbolSpec("USDJPY", "US Dollar vs Japanese Yen", "USD", "JPY", 151.50, 3, 100_000, 7e-5, 14),
    SimSymbolSpec("GOLD", "Gold vs US Dollar", "XAU", "USD", 2350.00, 2, 100, 1.4e-4, 30, stops_level=50),
)

GOLD_OPEN_MINUTES = (60, 23 * 60 + 57)  # XM: 01:00-23:57 server time, Monday to Friday

DEFAULT_SYMBOLS: tuple[SimSymbolSpec, ...] = (
    SimSymbolSpec("EURUSD", "Euro vs US Dollar", "EUR", "USD", 1.0850, 5, 100_000, 7e-5, 12, swap_long=-7.5, swap_short=1.9),
    SimSymbolSpec("GBPUSD", "Great Britain Pound vs US Dollar", "GBP", "USD", 1.2700, 5, 100_000, 8e-5, 16, swap_long=-3.2, swap_short=-2.4),
    SimSymbolSpec("USDJPY", "US Dollar vs Japanese Yen", "USD", "JPY", 151.50, 3, 100_000, 7e-5, 14, swap_long=9.8, swap_short=-24.1),
    SimSymbolSpec(
        "GOLD", "Gold vs US Dollar", "XAU", "USD", 2350.00, 2, 100, 1.4e-4, 30, stops_level=50,
        open_minutes=GOLD_OPEN_MINUTES, reopen_spread_points=70, swap_long=-86.84, swap_short=19.79,
    ),
    # XM BTCUSD (spec-btc-strategies D16): 1 BTC per lot, a fixed $40 spread, no daily break, swap
    # charged every night (it reports no valid triple day). The sim clock still skips weekends.
    SimSymbolSpec(
        "BTCUSD", "Bitcoin vs US Dollar", "BTC", "USD", 84_000.00, 2, 1, 6e-4, 4000, stops_level=0, volume_max=80.0,
        swap_long=-3500.13, swap_short=-2333.42, swap_rollover3days=7, stream=1,
    ),
)


def weekday(ts: int) -> int:
    """Monday=0 .. Sunday=6 for an epoch-seconds timestamp (1970-01-01 was a Thursday)."""
    return (ts // DAY + 3) % 7


def is_open(spec: SimSymbolSpec, ts):
    """Whether the M1 bar opening at ``ts`` (an int or an array) trades. Weekends are never generated."""
    if spec.open_minutes is None:
        return np.ones(np.shape(ts), dtype=bool) if isinstance(ts, np.ndarray) else True
    minute = (ts % DAY) // MINUTE
    return (minute >= spec.open_minutes[0]) & (minute < spec.open_minutes[1])


def next_market_minute(ts: int) -> int:
    nxt = ts + MINUTE
    wd = weekday(nxt)
    if wd >= 5:  # Saturday/Sunday -> Monday 00:00
        nxt = (nxt // DAY + (7 - wd)) * DAY
    return nxt


def default_start(now: datetime | None = None) -> int:
    """Most recent Monday 08:00 UTC — a busy weekday morning, reproducible within a week."""
    now = now or datetime.now(timezone.utc)
    monday = (now - timedelta(days=now.weekday())).replace(hour=8, minute=0, second=0, microsecond=0)
    if monday > now:
        monday -= timedelta(days=7)
    return int(monday.timestamp())


class _Series:
    """Growable M1 bar arrays for one Symbol."""

    def __init__(self, capacity: int):
        self.n = 0
        self.t = np.zeros(capacity, dtype=np.int64)
        self.o = np.zeros(capacity)
        self.h = np.zeros(capacity)
        self.l = np.zeros(capacity)
        self.c = np.zeros(capacity)
        self.v = np.zeros(capacity)

    def _grow(self) -> None:
        cap = len(self.t) * 2
        for name in ("t", "o", "h", "l", "c", "v"):
            arr = getattr(self, name)
            new = np.zeros(cap, dtype=arr.dtype)
            new[: self.n] = arr[: self.n]
            setattr(self, name, new)

    def append(self, t: int, o: float, h: float, l: float, c: float, v: float) -> None:
        if self.n == len(self.t):
            self._grow()
        i = self.n
        self.t[i], self.o[i], self.h[i], self.l[i], self.c[i], self.v[i] = t, o, h, l, c, v
        self.n += 1

    @property
    def last_close(self) -> float:
        return float(self.c[self.n - 1])


@dataclass
class _OpenPosition:
    ticket: int
    symbol: str
    side: Side
    volume: float
    price_open: float
    sl: float
    tp: float
    magic: int
    time: int
    comment: str


class SimBroker:
    mode = "sim"

    def __init__(
        self,
        seed: int = 42,
        start: int | None = None,
        history_days: int = 60,
        balance: float = 10_000.0,
        leverage: int = 500,
        symbols: tuple[SimSymbolSpec, ...] = DEFAULT_SYMBOLS,
        momentum: float = 0.0,
        deep_history_days: int = 0,
    ):
        """``momentum`` > 0 plants a genuine trend-following edge (AR(1) on 1-minute returns);
        0 is a random walk with slow drift regimes. Tests use it to prove learning can find an edge."""
        self._momentum = float(momentum)
        self._last_r: dict[str, float] = {}
        self._rng = np.random.default_rng(seed)
        self._specs = {s.name: s for s in symbols}
        self._own_rng = {s.name: np.random.default_rng([seed, 100 + s.stream]) for s in symbols if s.stream}
        self._balance = balance
        self._leverage = leverage
        self._connected = False
        self._next_ticket = 1_000_001
        self._positions: dict[int, _OpenPosition] = {}
        self._closed: list[ClosedTrade] = []
        self._series: dict[str, _Series] = {}
        self._drift: dict[str, float] = {}
        self._price: dict[str, float] = {}  # latest price, which keeps moving while a Symbol is shut
        self._deep: dict[str, pd.DataFrame] = {}  # H1 bars before the M1 history
        self.faults: list[str] = []
        self.hang_seconds = 2.0
        self.login = 99_000_001
        self.is_demo = True
        self.margin_mode = "hedging"
        self.algo_trading = True
        self.trade_modes: dict[str, str] = {}
        start = start if start is not None else default_start()
        self._generate_history(start, history_days)
        if deep_history_days > 0:
            self._generate_deep(seed, start - history_days * DAY, deep_history_days)
        self._last_bar_time = max(int(s.t[s.n - 1]) for s in self._series.values() if s.n)
        self.now = self._last_bar_time + MINUTE  # server time; every stored bar is closed

    # ----------------------------------------------------------------- history
    def _generate_history(self, start: int, days: int) -> None:
        first = (start - days * DAY) // MINUTE * MINUTE
        times = np.arange(first, start, MINUTE, dtype=np.int64)
        times = times[((times // DAY + 3) % 7) < 5]
        n = len(times)
        for spec in self._specs.values():
            rng = self._own_rng.get(spec.name, self._rng)
            noise = rng.standard_normal(n)
            drift_noise = rng.standard_normal(n)
            drift = pd.Series(drift_noise).ewm(alpha=1 / 400, adjust=False).mean().to_numpy()
            drift *= 0.9 * spec.sigma  # slow-moving trend component -> strategies see regimes
            rets = drift + spec.sigma * noise
            if self._momentum:
                for i in range(1, n):  # AR(1): r_t += momentum * r_{t-1}
                    rets[i] += self._momentum * rets[i - 1]
            # anchor the path so the *last* close is near the reference price
            log_path = np.cumsum(rets)
            closes = spec.price * np.exp(log_path - log_path[-1])
            opens = np.empty(n)
            opens[0] = closes[0]
            opens[1:] = closes[:-1]
            wick = np.abs(rng.standard_normal((2, n))) * spec.sigma * 0.6
            highs = np.maximum(opens, closes) * (1 + wick[0])
            lows = np.minimum(opens, closes) * (1 - wick[1])
            vols = rng.integers(20, 400, n).astype(float)
            # the random draws above are the same whether or not the Symbol has trading hours, so
            # other Symbols' paths do not depend on it; closed minutes are dropped afterwards
            keep = is_open(spec, times)
            m = int(keep.sum())
            s = _Series(capacity=max(1024, m * 2))
            d = spec.digits
            s.t[:m] = times[keep]
            s.o[:m], s.h[:m], s.l[:m], s.c[:m] = (np.round(a[keep], d) for a in (opens, highs, lows, closes))
            s.v[:m] = vols[keep]
            s.n = m
            self._series[spec.name] = s
            self._drift[spec.name] = float(drift[-1])
            self._last_r[spec.name] = float(rets[-1])
            self._price[spec.name] = round(float(closes[-1]), d)

    def _generate_deep(self, seed: int, m1_start: int, days: int) -> None:
        """H1 bars for ``days`` before ``m1_start``, from their own generator so the M1 history (and
        every test pinned on it) is unchanged. The path ends at the first M1 open."""
        shared = np.random.default_rng([seed, 1])
        end = m1_start // 3600 * 3600
        times = np.arange((end - days * DAY) // DAY * DAY, end, 3600, dtype=np.int64)
        times = times[((times // DAY + 3) % 7) < 5]
        for spec in self._specs.values():
            rng = np.random.default_rng([seed, 1, spec.stream]) if spec.stream else shared
            s = self._series[spec.name]
            ts = times
            if spec.open_minutes is not None:  # an hour trades if any of its minutes does
                first = (times % DAY) // MINUTE
                ts = times[(first + 60 > spec.open_minutes[0]) & (first < spec.open_minutes[1])]
            n = len(ts)
            if n == 0 or s.n == 0:
                continue
            sig = spec.sigma * math.sqrt(60)
            drift = pd.Series(rng.standard_normal(n)).ewm(alpha=1 / 20, adjust=False).mean().to_numpy() * 0.5 * sig
            log_path = np.cumsum(drift + sig * rng.standard_normal(n))
            closes = float(s.o[0]) * np.exp(log_path - log_path[-1])
            opens = np.r_[closes[0], closes[:-1]]
            wick = np.abs(rng.standard_normal((2, n))) * sig * 0.5
            d = spec.digits
            self._deep[spec.name] = pd.DataFrame(
                {
                    "time": ts,
                    "open": np.round(opens, d),
                    "high": np.round(np.maximum(opens, closes) * (1 + wick[0]), d),
                    "low": np.round(np.minimum(opens, closes) * (1 - wick[1]), d),
                    "close": np.round(closes, d),
                    "volume": rng.integers(1200, 24000, n).astype(float),
                }
            )

    # ------------------------------------------------------------ time control
    def step(self, bars: int = 1, shocks: dict[str, float] | None = None) -> int:
        """Advance the market by ``bars`` M1 bars. ``shocks`` forces a relative move on the first bar."""
        for i in range(bars):
            bar_time = next_market_minute(self._last_bar_time)
            self._last_bar_time = bar_time
            traded: set[str] = set()
            for name, spec in self._specs.items():
                s = self._series[name]
                o = self._price[name]
                rng = self._own_rng.get(name, self._rng)
                a = 1 / 400
                self._drift[name] = (1 - a) * self._drift[name] + a * 0.9 * spec.sigma * rng.standard_normal()
                r = self._drift[name] + spec.sigma * rng.standard_normal() + self._momentum * self._last_r.get(name, 0.0)
                self._last_r[name] = r
                if i == 0 and shocks and name in shocks:
                    # a shock is a price gap (news, weekend): the bar OPENS at the new level, so
                    # stops in between fill at the gap price, not at their own level
                    o = o * (1 + shocks[name])
                    r = 0.0
                c = o * math.exp(r)
                w1, w2 = np.abs(rng.standard_normal(2)) * spec.sigma * 0.6
                h = max(o, c) * (1 + w1)
                l = min(o, c) * (1 - w2)
                d = spec.digits
                vol = float(rng.integers(20, 400))
                self._price[name] = round(c, d)
                if is_open(spec, bar_time):  # a shut Symbol draws the same numbers but prints no bar
                    s.append(bar_time, round(o, d), round(h, d), round(l, d), round(c, d), vol)
                    traded.add(name)
            self.now = bar_time + MINUTE
            self._check_stops(traded)
        return self.now

    def shock(self, symbol: str, pct: float) -> int:
        self._spec(symbol)
        return self.step(1, shocks={symbol: pct})

    # --------------------------------------------------------------- lifecycle
    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def server_time(self) -> int:
        return self.now

    def terminal(self) -> TerminalStatus:
        return TerminalStatus(connected=self._connected, algo_trading=self.algo_trading, ping_ms=5.0, build=0, name="FXCommand Sim")

    def inject(self, kind: str, count: int = 1) -> None:
        if kind not in ENTRY_FAULTS + CLOSE_FAULTS:
            raise ValueError(f"unknown fault {kind!r}; one of {', '.join(ENTRY_FAULTS + CLOSE_FAULTS)}")
        self.faults.extend([kind] * count)

    def _take_fault(self, kinds: tuple[str, ...]) -> str | None:
        for i, f in enumerate(self.faults):
            if f in kinds:
                return self.faults.pop(i)
        return None

    # ----------------------------------------------------------------- queries
    def _spec(self, symbol: str) -> SimSymbolSpec:
        try:
            return self._specs[symbol]
        except KeyError:
            raise BrokerError(f"unknown symbol {symbol!r}") from None

    def _spread(self, symbol: str) -> int:
        spec = self._spec(symbol)
        minute_of_day = (self.now % DAY) // MINUTE
        if spec.reopen_spread_points is not None and spec.open_minutes is not None:
            if spec.open_minutes[0] <= minute_of_day < spec.open_minutes[0] + spec.reopen_minutes:
                return spec.reopen_spread_points
        rollover = minute_of_day >= 23 * 60 + 55 or minute_of_day < 10
        return spec.spread_points * (4 if rollover else 1)

    def market_open(self, symbol: str) -> bool:
        """Whether the Symbol trades now (in the minute that is forming)."""
        return weekday(self.now) < 5 and bool(is_open(self._spec(symbol), self.now))

    def _tick_value(self, spec: SimSymbolSpec, bid: float) -> float:
        tick_size = spec.point
        if spec.quote == "USD":
            return spec.contract_size * tick_size
        return spec.contract_size * tick_size / bid  # USD-based pair, e.g. USDJPY

    def symbols(self) -> list[str]:
        return list(self._specs)

    def symbol_info(self, symbol: str) -> SymbolInfo:
        spec = self._spec(symbol)
        bid = self._series[symbol].last_close
        return SymbolInfo(
            name=spec.name,
            description=spec.description,
            digits=spec.digits,
            point=spec.point,
            trade_tick_size=spec.point,
            trade_tick_value=self._tick_value(spec, bid),
            contract_size=spec.contract_size,
            volume_min=spec.volume_min,
            volume_max=spec.volume_max,
            volume_step=spec.volume_step,
            stops_level=spec.stops_level,
            filling_mode=1,
            trade_mode=self.trade_modes.get(symbol, "full"),
            swap_long=spec.swap_long,
            swap_short=spec.swap_short,
            swap_mode=spec.swap_mode,
            swap_rollover3days=spec.swap_rollover3days,
        )

    def tick(self, symbol: str) -> Tick:
        spec = self._spec(symbol)
        s = self._series[symbol]
        bid = s.last_close
        ask = round(bid + self._spread(symbol) * spec.point, spec.digits)
        # while a Symbol is shut its last quote is the last bar's close, and it ages
        return Tick(symbol=symbol, time=min(self.now, int(s.t[s.n - 1]) + MINUTE), bid=bid, ask=ask, point=spec.point)

    def closed_bars(self, symbol: str, timeframe: Timeframe, count: int, offset: int = 0) -> pd.DataFrame:
        if offset <= 0:
            return self._closed_bars(symbol, timeframe, count)
        bars = self._closed_bars(symbol, timeframe, count + offset)
        return bars.iloc[: max(0, len(bars) - offset)].reset_index(drop=True)

    def _closed_bars(self, symbol: str, timeframe: Timeframe, count: int) -> pd.DataFrame:
        self._spec(symbol)
        s = self._series[symbol]
        tf = Timeframe(timeframe)
        sec = tf.seconds
        per = sec // MINUTE
        want = (count + 2) * per + (3 * DAY // MINUTE if sec >= 3600 else 0)
        if want > s.n and sec >= 3600 and symbol in self._deep:
            return self._closed_bars_deep(symbol, sec, count)
        k = min(s.n, want)
        start = s.n - k
        t = s.t[start : s.n]
        if k == 0:
            return empty_bars()
        if per == 1:
            sl = slice(max(0, k - count), k)
            return pd.DataFrame(
                {
                    "time": t[sl],
                    "open": s.o[start : s.n][sl],
                    "high": s.h[start : s.n][sl],
                    "low": s.l[start : s.n][sl],
                    "close": s.c[start : s.n][sl],
                    "volume": s.v[start : s.n][sl],
                }
            )
        bucket = t // sec * sec
        idx = np.flatnonzero(np.r_[True, bucket[1:] != bucket[:-1]])
        ends = np.r_[idx[1:] - 1, k - 1]
        o = s.o[start : s.n][idx]
        c = s.c[start : s.n][ends]
        h = np.maximum.reduceat(s.h[start : s.n], idx)
        l = np.minimum.reduceat(s.l[start : s.n], idx)
        v = np.add.reduceat(s.v[start : s.n], idx)
        bt = bucket[idx]
        keep = np.ones(len(idx), dtype=bool)
        if start > 0:
            keep[0] = False  # first bucket may be cut by the window
        if bt[-1] + sec > self.now:
            keep[-1] = False  # still forming
        df = pd.DataFrame({"time": bt[keep], "open": o[keep], "high": h[keep], "low": l[keep], "close": c[keep], "volume": v[keep]})
        return df.iloc[-count:].reset_index(drop=True)

    def _closed_bars_deep(self, symbol: str, sec: int, count: int) -> pd.DataFrame:
        """H1-or-longer bars reaching into the deep H1 history: the M1 history is rolled up to H1,
        appended to the deep bars and then rolled up to the Timeframe."""
        s = self._series[symbol]
        t = s.t[: s.n]
        hour = t // 3600 * 3600
        idx = np.flatnonzero(np.r_[True, hour[1:] != hour[:-1]])
        ends = np.r_[idx[1:] - 1, s.n - 1]
        recent = pd.DataFrame(
            {
                "time": hour[idx],
                "open": s.o[: s.n][idx],
                "high": np.maximum.reduceat(s.h[: s.n], idx),
                "low": np.minimum.reduceat(s.l[: s.n], idx),
                "close": s.c[: s.n][ends],
                "volume": np.add.reduceat(s.v[: s.n], idx),
            }
        )
        bars = pd.concat([self._deep[symbol], recent], ignore_index=True)
        if sec != 3600:
            bucket = bars["time"].to_numpy() // sec * sec
            bars = bars.groupby(bucket, sort=True).agg(
                open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"), volume=("volume", "sum")
            )
            bars = bars.rename_axis("time").reset_index()
        bars = bars[bars["time"] + sec <= self.now]  # drop the forming bar
        return bars.iloc[-count:].reset_index(drop=True)

    def account(self) -> AccountInfo:
        floating = sum(self._profit(p) for p in self._positions.values())
        margin = sum(self._margin(p.symbol, p.volume) for p in self._positions.values())
        equity = self._balance + floating
        return AccountInfo(
            login=self.login,
            name="Simulated Account",
            server="FXCommand-Sim",
            company="FXCommand",
            currency="USD",
            balance=round(self._balance, 2),
            equity=round(equity, 2),
            margin=round(margin, 2),
            margin_free=round(equity - margin, 2),
            leverage=self._leverage,
            is_demo=self.is_demo,
            trade_allowed=self.algo_trading,
            margin_mode=self.margin_mode,
        )

    def positions(self, magic: int | None = None) -> list[Position]:
        out = []
        for p in self._positions.values():
            if magic is not None and p.magic != magic:
                continue
            t = self.tick(p.symbol)
            out.append(
                Position(
                    ticket=p.ticket,
                    symbol=p.symbol,
                    side=p.side,
                    volume=p.volume,
                    price_open=p.price_open,
                    price_current=t.bid if p.side == "long" else t.ask,
                    sl=p.sl,
                    tp=p.tp,
                    profit=round(self._profit(p), 2),
                    magic=p.magic,
                    time=p.time,
                    comment=p.comment,
                )
            )
        return out

    def history(self, since: int) -> list[ClosedTrade]:
        return [c for c in self._closed if c.time_close >= since]

    # ------------------------------------------------------------------ money
    def _pnl(self, p: _OpenPosition, exit_price: float) -> float:
        spec = self._spec(p.symbol)
        diff = exit_price - p.price_open if p.side == "long" else p.price_open - exit_price
        return diff / spec.point * self._tick_value(spec, self._series[p.symbol].last_close) * p.volume

    def _profit(self, p: _OpenPosition) -> float:
        t = self.tick(p.symbol)
        return self._pnl(p, t.bid if p.side == "long" else t.ask)

    def _margin(self, symbol: str, volume: float) -> float:
        spec = self._spec(symbol)
        notional = volume * spec.contract_size
        if spec.base != "USD":
            notional *= self._series[symbol].last_close
        return notional / self._leverage

    # ----------------------------------------------------------------- orders
    def _stops_error(self, spec: SimSymbolSpec, side: Side, sl: float, tp: float, t: Tick) -> str | None:
        min_dist = spec.stops_level * spec.point
        if side == "long":
            if not sl or sl > t.bid - min_dist:
                return f"invalid stops: SL {sl} must be <= bid {t.bid} - {spec.stops_level} points"
            if tp and tp < t.bid + min_dist:
                return f"invalid stops: TP {tp} must be >= bid {t.bid} + {spec.stops_level} points"
        else:
            if not sl or sl < t.ask + min_dist:
                return f"invalid stops: SL {sl} must be >= ask {t.ask} + {spec.stops_level} points"
            if tp and tp > t.ask - min_dist:
                return f"invalid stops: TP {tp} must be <= ask {t.ask} - {spec.stops_level} points"
        return None

    def market_order(
        self, symbol: str, side: Side, volume: float, sl: float, tp: float, magic: int, comment: str = ""
    ) -> OrderResult:
        if side not in ("long", "short"):
            return OrderResult(False, RET_INVALID, f"invalid side {side!r}")
        spec = self._spec(symbol)
        steps = volume / spec.volume_step
        if volume < spec.volume_min or volume > spec.volume_max or abs(steps - round(steps)) > 1e-6:
            return OrderResult(False, RET_INVALID_VOLUME, f"invalid volume {volume}")
        t = self.tick(symbol)
        err = self._stops_error(spec, side, sl, tp, t)
        if err:
            return OrderResult(False, RET_INVALID_STOPS, err)
        if self._margin(symbol, volume) > self.account().margin_free:
            return OrderResult(False, RET_NO_MONEY, "not enough money")
        if not self.market_open(symbol):
            return OrderResult(False, RET_MARKET_CLOSED, "market closed")
        mode = self.trade_modes.get(symbol, "full")
        if mode in ("disabled", "closeonly") or (mode == "longonly" and side == "short") or (mode == "shortonly" and side == "long"):
            return OrderResult(False, RET_MARKET_CLOSED, f"trade mode {mode}")
        fault = self._take_fault(ENTRY_FAULTS)
        if fault == "hang":
            import time as _time

            _time.sleep(self.hang_seconds)
        if fault == "requote":
            return OrderResult(False, RET_REQUOTE, "requote")
        if fault == "timeout_none":
            return OrderResult(False, RET_TIMEOUT, "request timeout", uncertain=True)
        price = t.ask if side == "long" else t.bid
        if fault == "partial":
            half = math.floor(volume / 2 / spec.volume_step) * spec.volume_step
            volume = max(spec.volume_min, round(half, 2))
        ticket = self._next_ticket
        self._next_ticket += 1
        self._positions[ticket] = _OpenPosition(ticket, symbol, side, round(volume, 2), price, sl, tp or 0.0, magic, self.now, comment)
        if fault == "timeout_filled":  # the order executed but the answer was lost
            return OrderResult(False, RET_TIMEOUT, "request timeout", uncertain=True)
        if fault == "price_zero":
            return OrderResult(True, RET_DONE, "done", ticket=ticket, price=0.0, volume=round(volume, 2))
        code = RET_DONE_PARTIAL if fault == "partial" else RET_DONE
        return OrderResult(True, code, "done", ticket=ticket, price=price, volume=round(volume, 2))

    def margin_required(self, symbol: str, side: Side, volume: float) -> float:
        return round(self._margin(symbol, volume), 2)

    def modify(self, ticket: int, sl: float, tp: float) -> OrderResult:
        p = self._positions.get(ticket)
        if p is None:
            return OrderResult(False, RET_POSITION_NOT_FOUND, f"position {ticket} not found")
        spec = self._spec(p.symbol)
        if not self.market_open(p.symbol):
            return OrderResult(False, RET_MARKET_CLOSED, "market closed")
        err = self._stops_error(spec, p.side, sl, tp, self.tick(p.symbol))
        if err:
            return OrderResult(False, RET_INVALID_STOPS, err)
        p.sl, p.tp = sl, tp or 0.0
        return OrderResult(True, RET_DONE, "done", ticket=ticket)

    def close(self, ticket: int, comment: str = "") -> OrderResult:
        p = self._positions.get(ticket)
        if p is None:
            return OrderResult(False, RET_POSITION_NOT_FOUND, f"position {ticket} not found")
        if not self.market_open(p.symbol):
            return OrderResult(False, RET_MARKET_CLOSED, "market closed")
        fault = self._take_fault(CLOSE_FAULTS)
        if fault == "close_fail":
            return OrderResult(False, RET_PRICE_OFF, "off quotes")
        t = self.tick(p.symbol)
        price = t.bid if p.side == "long" else t.ask
        self._settle(p, price, "manual" if comment == "manual" else "expert")
        if fault == "close_timeout_done":
            return OrderResult(False, RET_TIMEOUT, "request timeout", uncertain=True)
        return OrderResult(True, RET_DONE, "done", ticket=ticket, price=price, volume=p.volume)

    def _settle(self, p: _OpenPosition, price: float, reason: str) -> None:
        profit = round(self._pnl(p, price), 2)
        self._balance += profit
        del self._positions[p.ticket]
        self._closed.append(
            ClosedTrade(
                ticket=p.ticket,
                symbol=p.symbol,
                side=p.side,
                volume=p.volume,
                price_open=p.price_open,
                price_close=price,
                profit=profit,
                magic=p.magic,
                time_open=p.time,
                time_close=self.now,
                reason=reason,
            )
        )

    def _check_stops(self, traded: set[str]) -> None:
        """Fire SL/TP against the bar that just closed. If both are touched, assume SL (conservative).
        A shut Symbol printed no bar, so its stops wait for the reopen (and fill at the gap)."""
        for p in list(self._positions.values()):
            if p.symbol not in traded:
                continue
            s = self._series[p.symbol]
            i = s.n - 1
            o, h, l = s.o[i], s.h[i], s.l[i]
            spread = self._spread(p.symbol) * self._spec(p.symbol).point
            if p.side == "long":
                if p.sl and l <= p.sl:
                    self._settle(p, min(p.sl, o), "sl")
                elif p.tp and h >= p.tp:
                    self._settle(p, max(p.tp, o), "tp")
            else:
                if p.sl and h + spread >= p.sl:
                    self._settle(p, max(p.sl, o + spread), "sl")
                elif p.tp and l + spread <= p.tp:
                    self._settle(p, min(p.tp, o + spread), "tp")

    # ------------------------------------------------------------------ misc
    def state(self) -> dict:
        return {
            "server_time": self.now,
            "open_positions": len(self._positions),
            "closed_trades": len(self._closed),
            "balance": round(self._balance, 2),
            "faults": list(self.faults),
            "login": self.login,
            "is_demo": self.is_demo,
            "margin_mode": self.margin_mode,
            "prices": {k: s.last_close for k, s in self._series.items()},
            "market_open": {k: self.market_open(k) for k in self._specs},
        }
