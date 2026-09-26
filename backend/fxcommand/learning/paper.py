"""PaperTrader: the Shadow Trade rules, one bar at a time. ``backtest`` replays a series through it.

Rules mirror live trading (ADR 0004):

- A Signal on the close of bar t is acted on at the open of bar t+1 (the live engine sends
  its order right after the close). Longs fill at the ask (bid + spread), shorts at the bid.
- Stop-loss / take-profit are checked from the entry bar onwards, never on the Signal bar.
  If a bar touches both, the stop-loss is assumed first; a gap through the stop fills at the open.
- Strategy exits and reverse-on-opposite follow the live ``SessionManager._act`` order:
  an exit Signal closes and nothing else happens on that bar.
- Breakeven / ATR trailing are applied at each bar close (live applies them every engine pass,
  so live can tighten slightly earlier — documented tolerance).
- Costs: spread on entry via the ask, plus ``slippage`` on market fills and stop fills, both at the
  bar's price (a ``CostModel`` scales them with price, so old cheap years are not overcharged), and
  overnight swap for every rollover the position was held through.
- The Trading Window is judged when the entry fills (the next bar's open, the next tradable time),
  not when the Signal fires: an entry is taken if the window opens within the fill limit (the bar,
  or the Strategy's shorter fill window), as a live Pending Entry would be.

Everything is measured in R: result / initial stop distance.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from ..risk.window import DAY, TradingWindow, fill_limit, next_tradable
from ..strategies import get_strategy
from .candidate import Candidate
from .costs import CostModel


@dataclass(frozen=True)
class Costs:
    """Fixed costs (tests, simple callers). ``CostModel`` has the same methods, price-scaled and with swap."""

    spread: float  # price units (ask - bid)
    slippage: float = 0.0  # price units per market/stop fill

    def spread_at(self, price: float) -> float:
        return self.spread

    def slippage_at(self, price: float) -> float:
        return self.slippage

    def swap_for(self, side: str, entry_price: float, open_ts: int, close_ts: int) -> float:
        return 0.0


CostLike = Costs | CostModel


class EntryGate:
    """Whether an entry filling at the open of a bar may be taken: the Trading Window must be open
    at the next tradable time, and that must fall within the fill limit (the bar, or the Strategy's
    shorter ``fill_window_s``). Plain data only, so it pickles into an Optimizer Run process."""

    def __init__(self, window: dict | None, bar_seconds: int, fill_window_s: int | None = None):
        self.window_dict = window
        self.bar_seconds = int(bar_seconds)
        self.fill_window_s = fill_window_s
        self._window = TradingWindow.from_dict(window) if window is not None else None

    def for_strategy(self, key: str) -> "EntryGate":
        return EntryGate(self.window_dict, self.bar_seconds, get_strategy(key).fill_window_s)

    @property
    def limit(self) -> int:
        return fill_limit(self.bar_seconds, self.fill_window_s)

    def __call__(self, t: int) -> bool:
        if self._window is None or self._window.is_open(t):
            return True
        nt = next_tradable(t, self._window, horizon_days=self.limit / DAY)  # scans only while shut
        return nt is not None and nt < t + self.limit

    def __getstate__(self):
        return {"window": self.window_dict, "bar_seconds": self.bar_seconds, "fill_window_s": self.fill_window_s}

    def __setstate__(self, d):
        self.__init__(d["window"], d["bar_seconds"], d["fill_window_s"])


@dataclass(frozen=True)
class ExitRules:
    breakeven: bool = False
    breakeven_at_r: float = 1.0
    trailing: bool = False
    trailing_atr: float = 2.0
    trailing_start_r: float = 1.0
    reverse_on_opposite: bool = True

    @classmethod
    def from_profile(cls, profile, reverse_on_opposite: bool = True) -> "ExitRules":
        return cls(
            breakeven=profile.breakeven,
            breakeven_at_r=profile.breakeven_at_r,
            trailing=profile.trailing,
            trailing_atr=profile.trailing_atr,
            trailing_start_r=profile.trailing_start_r,
            reverse_on_opposite=reverse_on_opposite,
        )


@dataclass
class OpenPaper:
    side: str
    entry: float
    sl: float
    tp: float
    risk: float  # initial stop distance in price (1R)
    open_time: int
    signal_time: int
    reason: str = ""
    features: list[float] | None = None
    p_win: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PaperTrade:
    side: str
    open_time: int
    close_time: int
    signal_time: int
    entry: float
    exit: float
    r: float
    reason: str  # sl | tp | exit | reverse | end
    features: list[float] | None = None
    p_win: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Pending:
    side: str
    sl_dist: float
    tp_dist: float
    signal_time: int
    features: list[float] | None
    p_win: float | None


# make_features(side, signal_bar_index_or_time) -> (features, p_win)
FeatureFn = Callable[[str], tuple[list[float] | None, float | None]]


@dataclass
class PaperTrader:
    costs: CostLike
    rules: ExitRules = field(default_factory=ExitRules)
    allow_entry: Callable[[int], bool] | None = None  # judged at the fill time (e.g. an EntryGate)
    position: OpenPaper | None = None
    last_r: float = 0.0
    _close: str | None = None
    _entry: _Pending | None = None

    # ------------------------------------------------------------ helpers
    def _r(self, p: OpenPaper, exit_price: float) -> float:
        diff = exit_price - p.entry if p.side == "long" else p.entry - exit_price
        return diff / p.risk if p.risk > 0 else 0.0

    def _settle(self, t: int, exit_price: float, reason: str) -> PaperTrade:
        p = self.position
        assert p is not None
        swap = self.costs.swap_for(p.side, p.entry, p.open_time, t)  # signed price units, + earns
        r = self._r(p, exit_price) + (swap / p.risk if p.risk > 0 else 0.0)
        self.position = None
        self.last_r = r
        return PaperTrade(p.side, p.open_time, t, p.signal_time, p.entry, exit_price, r, reason, p.features, p.p_win)

    def _market_exit(self, o: float) -> float:
        p = self.position
        slip = self.costs.slippage_at(o)
        return o - slip if p.side == "long" else o + self.costs.spread_at(o) + slip

    # ------------------------------------------------------------ one bar
    def on_bar(
        self,
        t: int,
        o: float,
        h: float,
        l: float,
        c: float,
        atr: float,
        sig: dict,
        make_features: FeatureFn | None = None,
    ) -> tuple[OpenPaper | None, list[PaperTrade]]:
        """Process bar t (just closed). Returns (position opened at this bar's open, trades closed)."""
        closed: list[PaperTrade] = []
        opened: OpenPaper | None = None
        spread, slip = self.costs.spread_at(o), self.costs.slippage_at(o)

        # 1. orders scheduled at the previous close fill at this open
        if self._close and self.position:
            closed.append(self._settle(t, self._market_exit(o), self._close))
        self._close = None
        if self._entry and self.position is None and (self.allow_entry is None or self.allow_entry(t)):
            e = self._entry
            if e.side == "long":
                entry = o + spread + slip
                sl, tp = entry - e.sl_dist, (entry + e.tp_dist if e.tp_dist > 0 else 0.0)
            else:
                entry = o - slip
                sl, tp = entry + e.sl_dist, (entry - e.tp_dist if e.tp_dist > 0 else 0.0)
            self.position = OpenPaper(e.side, entry, sl, tp, e.sl_dist, t, e.signal_time, "", e.features, e.p_win)
            opened = self.position
        self._entry = None

        # 2. stops during this bar (entry bar included; the Signal bar never is)
        p = self.position
        if p is not None:
            if p.side == "long":
                if o <= p.sl:
                    closed.append(self._settle(t, o - slip, "sl"))
                elif l <= p.sl:
                    closed.append(self._settle(t, p.sl - slip, "sl"))
                elif p.tp and h >= p.tp:
                    closed.append(self._settle(t, p.tp, "tp"))
            else:
                if o + spread >= p.sl:
                    closed.append(self._settle(t, o + spread + slip, "sl"))
                elif h + spread >= p.sl:
                    closed.append(self._settle(t, p.sl + slip, "sl"))
                elif p.tp and l + spread <= p.tp:
                    closed.append(self._settle(t, p.tp, "tp"))

        # 3. stop management at the close
        p = self.position
        if p is not None and (self.rules.breakeven or self.rules.trailing):
            price = c if p.side == "long" else c + spread
            profit = price - p.entry if p.side == "long" else p.entry - price
            cands = []
            if self.rules.breakeven and profit >= self.rules.breakeven_at_r * p.risk:
                cands.append(p.entry)
            if self.rules.trailing and atr > 0 and profit >= self.rules.trailing_start_r * p.risk:
                cands.append(price - self.rules.trailing_atr * atr if p.side == "long" else price + self.rules.trailing_atr * atr)
            if cands:
                if p.side == "long":
                    p.sl = max(p.sl, max(cands))
                else:
                    p.sl = min(p.sl, min(cands))

        # 4. the Strategy's decision on this close (same precedence as the live engine)
        p = self.position
        if p is not None and ((p.side == "long" and sig["exit_long"]) or (p.side == "short" and sig["exit_short"])):
            self._close = "exit"
            return opened, closed
        side = "long" if sig["long"] else "short" if sig["short"] else None
        if side is None:
            return opened, closed
        if p is not None:
            if p.side == side:
                return opened, closed
            self._close = "reverse"
            if not self.rules.reverse_on_opposite:
                return opened, closed
        sl_dist, tp_dist = float(sig["sl_dist"]), float(sig["tp_dist"])
        if not (sl_dist > 0 and np.isfinite(sl_dist)):
            return opened, closed
        feats, pw = make_features(side) if make_features else (None, None)
        self._entry = _Pending(side, sl_dist, tp_dist if np.isfinite(tp_dist) else 0.0, t, feats, pw)
        return opened, closed

    def finish(self, t: int, c: float) -> list[PaperTrade]:
        """Close whatever is open at the last close (end of a Backtest)."""
        if self.position is None:
            return []
        return [self._settle(t, self._market_exit(c), "end")]

    # ------------------------------------------------------------ persistence
    def snapshot(self) -> dict:
        return {"position": self.position.to_dict() if self.position else None, "last_r": self.last_r}

    def restore(self, d: dict | None) -> None:
        if not d:
            return
        self.position = OpenPaper(**d["position"]) if d.get("position") else None
        self.last_r = float(d.get("last_r", 0.0))


def backtest(
    bars: pd.DataFrame,
    candidate: Candidate,
    costs: CostLike,
    rules: ExitRules | None = None,
    allow_entry: Callable[[int], bool] | None = None,
    features: pd.DataFrame | None = None,
    scorer: Callable[[list[float]], float] | None = None,
) -> list[PaperTrade]:
    """Replay ``bars`` through a PaperTrader for ``candidate``. Deterministic and pure."""
    from .features import vector

    bars = bars.reset_index(drop=True)
    if len(bars) == 0:
        return []
    frame = get_strategy(candidate.strategy).signals(bars, candidate.param_dict)
    t = bars["time"].to_numpy(dtype=np.int64)
    o, h, l, c = (bars[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    atr = frame["info_atr"].to_numpy(dtype=float)
    cols = {k: frame[k].to_numpy() for k in ("long", "short", "exit_long", "exit_short", "sl_dist", "tp_dist")}
    if isinstance(allow_entry, EntryGate):
        allow_entry = allow_entry.for_strategy(candidate.strategy)
    trader = PaperTrader(costs, rules or ExitRules(), allow_entry)
    trades: list[PaperTrade] = []
    for i in range(len(bars)):
        sig = {k: v[i] for k, v in cols.items()}
        mf = None
        if features is not None:

            def mf(side, i=i):  # evaluated only when an entry is actually scheduled
                vec = vector(features.iloc[i], side, costs.spread_at(float(c[i])), trader.last_r)
                return vec, (scorer(vec) if scorer else None)

        _, closed = trader.on_bar(int(t[i]), float(o[i]), float(h[i]), float(l[i]), float(c[i]), float(atr[i]) if atr[i] == atr[i] else 0.0, sig, mf)
        trades.extend(closed)
    trades.extend(trader.finish(int(t[-1]), float(c[-1])))
    return trades
