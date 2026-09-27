"""Evidence Run: does an edge exist for an Arena? (spec D11, D23, D24, D9)

A read-only Backtest of one Candidate on the connected feed's full history, priced with the current
``CostModel`` at twice the typical spread, with the Session's exit rules, Trading Window and Weekend
Close. Everything the result depends on is in its key, so the editor can tell "evidence for these
settings" from "evidence for other settings".

This module is pure: the key, the job the learning process pool runs, per-period results, and the
graduation scorecard's arithmetic (the band a Paper record should fall in, and the real Account's
minimum-lot risk). Fetching history and scheduling live in ``LearningService``.
"""

from __future__ import annotations

import calendar
import hashlib
import json
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from ..risk.window import TradingWindow, WeekendClose
from .candidate import Candidate
from .costs import COST_MODEL_VERSION, CostModel, rollover_nights
from .objective import r_stats
from .paper import EntryGate, ExitRules, backtest

CHUNK_BARS = 10_000  # bars per broker call: each read stays far inside the broker timeout
CHUNK_TIMEOUT = 10.0  # seconds; a slower read marks the run incomplete instead of holding the engine
MAX_EVIDENCE_BARS = 400_000  # the most recent bars kept (years of M5; M1 is capped to about a year)
SPREAD_MULTIPLIER = 2.0  # Evidence is priced at twice today's typical spread
BAND_LEVEL = 0.90
BAND_DRAWS = 2000
MIN_SCORECARD_TRADES = 10  # fewer Paper trades say nothing either way
FIXED_PERIODS = (("2010–17", 2010, 2018), ("2018–21", 2018, 2022), ("2022–26", 2022, 2027))


def _year(y: int) -> int:
    return calendar.timegm((y, 1, 1, 0, 0, 0))


def _canonical(obj) -> object:
    if isinstance(obj, float):
        return round(obj, 6)
    if isinstance(obj, dict):
        return {k: _canonical(v) for k, v in sorted(obj.items())}
    if isinstance(obj, (list, tuple)):
        return [_canonical(v) for v in obj]
    return obj


def settings_hash(d: dict | None) -> str:
    blob = json.dumps(_canonical(d or {}), sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


@dataclass(frozen=True)
class EvidenceSpec:
    """What an Evidence Run tests. Build it with ``of`` so equal settings always give an equal key."""

    symbol: str
    timeframe: str
    candidate: Candidate
    rules: ExitRules = field(default_factory=ExitRules)
    window: dict = field(default_factory=dict)  # canonical TradingWindow dict
    weekend_close: str = ""  # "" = off, else the Friday close time
    cost_version: int = COST_MODEL_VERSION

    @classmethod
    def of(
        cls,
        symbol: str,
        timeframe: str,
        strategy: str,
        params: dict | None = None,
        rules: ExitRules | None = None,
        window: dict | None = None,
        weekend_close: str | None = None,
    ) -> "EvidenceSpec":
        """Defaults: no breakeven and no trailing stop, the default Trading Window, Weekend Close off."""
        return cls(
            symbol=symbol,
            timeframe=str(timeframe),
            candidate=Candidate.of(strategy, params),
            rules=rules or ExitRules(),
            window=TradingWindow.from_dict(window).to_dict(),
            weekend_close=weekend_close or "",
        )

    def with_weekend_close(self, close_time: str | None) -> "EvidenceSpec":
        return EvidenceSpec(self.symbol, self.timeframe, self.candidate, self.rules, self.window, close_time or "", self.cost_version)

    @property
    def rules_hash(self) -> str:
        return settings_hash(asdict(self.rules))

    @property
    def window_hash(self) -> str:
        return settings_hash(self.window)

    @property
    def key(self) -> str:
        return "|".join(
            (self.symbol, self.timeframe, self.candidate.key, self.rules_hash, self.weekend_close or "off", self.window_hash, f"c{self.cost_version}")
        )

    def row_fields(self) -> dict:
        return {
            "key": self.key,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "strategy": self.candidate.strategy,
            "candidate_key": self.candidate.key,
            "params": self.candidate.param_dict,
            "exit_rules": asdict(self.rules),
            "exit_rules_hash": self.rules_hash,
            "window": self.window,
            "window_hash": self.window_hash,
            "weekend_close": self.weekend_close,
            "cost_version": self.cost_version,
        }

    @classmethod
    def from_row(cls, row) -> "EvidenceSpec":
        return cls(
            row.symbol, row.timeframe, Candidate.of(row.strategy, row.params), ExitRules(**row.exit_rules),
            row.window, row.weekend_close, row.cost_version,
        )


def periods(close_ts: np.ndarray, rs: np.ndarray, first_ts: int, last_ts: int) -> list[dict]:
    """Mean R per period: 2010–17, 2018–21 and 2022–26 when the history reaches back to 2011,
    otherwise the history split into thirds. A trade belongs to the period it closed in."""
    if first_ts <= _year(2011):
        spans = [(label, max(_year(a), first_ts), min(_year(b), last_ts + 1)) for label, a, b in FIXED_PERIODS]
        spans = [s for s in spans if s[1] < s[2]]
    else:
        edges = [first_ts + (last_ts + 1 - first_ts) * k // 3 for k in range(4)]
        fmt = lambda ts: pd.Timestamp(ts, unit="s").strftime("%Y-%m")  # noqa: E731
        spans = [(f"{fmt(edges[k])} – {fmt(edges[k + 1] - 1)}", edges[k], edges[k + 1]) for k in range(3)]
    out = []
    for label, a, b in spans:
        sel = rs[(close_ts >= a) & (close_ts < b)]
        out.append({"label": label, "from_ts": int(a), "to_ts": int(b), "n": int(len(sel)), "mean": round(float(sel.mean()), 4) if len(sel) else 0.0})
    return out


def evidence_job(
    bars: pd.DataFrame,
    candidate: Candidate,
    costs: CostModel,
    rules: ExitRules,
    gate: EntryGate,
    weekend: WeekendClose | None,
) -> dict:
    """The Backtest and its statistics. Top level and made of plain data, so it runs in the learning
    process pool (keep it picklable: no lambdas)."""
    trades = backtest(bars, candidate, costs, rules, gate, weekend=weekend)
    rs = np.array([t.r for t in trades], dtype=float)
    close_ts = np.array([t.close_time for t in trades], dtype=np.int64)
    st = r_stats(rs)
    first, last = (int(bars["time"].iloc[0]), int(bars["time"].iloc[-1])) if len(bars) else (0, 0)
    nights = [rollover_nights(t.open_time, t.close_time, costs.triple_weekday) for t in trades]
    return {
        "avg_nights": round(float(np.mean(nights)), 3) if trades else None,  # swap nights per trade (the Cost Check shows swap)
        "long_share": round(sum(t.side == "long" for t in trades) / len(trades), 3) if trades else None,
        "bars": int(len(bars)),
        "first_ts": first,
        "last_ts": last,
        "trades": st.n,
        "mean_r": round(st.mean, 4),
        "sqn": round(st.sqn, 3),
        "win_rate": round(st.win_rate, 4),
        "total_r": round(st.total, 3),
        "max_dd_r": round(st.max_dd, 3),
        "periods": periods(close_ts, rs, first, last) if len(bars) else [],
        "rs": [round(float(r), 3) for r in rs],
    }


def band(rs: list[float] | np.ndarray, n: int, level: float = BAND_LEVEL, draws: int = BAND_DRAWS, seed: int = 7) -> tuple[float, float] | None:
    """Where the mean R of ``n`` trades drawn from the backtest's trades falls ``level`` of the time
    (bootstrap, fixed seed so a scorecard reads the same twice)."""
    a = np.asarray(rs, dtype=float)
    if n <= 0 or len(a) == 0:
        return None
    rng = np.random.default_rng(seed)
    means = a[rng.integers(0, len(a), size=(draws, n))].mean(axis=1)
    tail = (1 - level) / 2 * 100
    return round(float(np.percentile(means, tail)), 4), round(float(np.percentile(means, 100 - tail)), 4)


def verdict(n: int, mean: float, rng: tuple[float, float] | None) -> str:
    if rng is None:
        return "no evidence"
    if n < MIN_SCORECARD_TRADES:
        return "too few trades"
    if mean < rng[0]:
        return "below the backtest"
    if mean > rng[1]:
        return "above the backtest"
    return "within the backtest"


def min_lot_risk_pct(volume_min: float, stop: float, value_per_price: float, balance: float) -> float | None:
    """What one minimum-lot trade risks at this stop, as % of the real Account's balance."""
    if not balance or balance <= 0 or not stop or stop != stop:
        return None
    return round(volume_min * stop * value_per_price / balance * 100, 2)


def summary(row, with_rs: bool = False) -> dict:
    d = row.model_dump(exclude=None if with_rs else {"rs"})
    d["label"] = Candidate.of(row.strategy, row.params).label()
    return d
