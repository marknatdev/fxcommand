"""How Candidates are scored: statistics in R, the SQN-style objective, Walk-forward evaluation,
robustness, and the Guardrails that decide whether a Challenger may be promoted.

Calibration constants live here, in one place, and are pinned by tests on a pure-noise
market (nothing may be promotable) and on a planted-edge market (the edge must be found).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Iterable

import numpy as np

from .paper import PaperTrade

SQN_CAP = 100  # trades beyond this stop increasing confidence (classic SQN convention)
MIN_SHADOW_TRADES = 30
MIN_OOS_TRADES = 30
SHADOW_MIN_SQN = 1.0  # challenger's shadow SQN must at least look non-random
SHADOW_MIN_EDGE_R = 0.10  # challenger's mean R must beat the champion's by this much on the same bars
DEFLATE_K = 0.5  # share of the expected-maximum-of-noise penalty applied to Walk-forward scores
MAX_DD_RATIO = 1.2  # challenger drawdown may be at most this × champion's (+1R floor)
ROBUST_SHARE = 0.5  # neighbours' median OOS score must reach this share of the candidate's
ROLLBACK_TRADES = 20
ROLLBACK_MARGIN_R = 0.25


@dataclass(frozen=True)
class RStats:
    n: int
    mean: float
    std: float
    sqn: float
    win_rate: float
    total: float
    max_dd: float  # largest peak-to-trough fall of cumulative R

    def to_dict(self) -> dict:
        return asdict(self)


def r_stats(rs: Iterable[float]) -> RStats:
    a = np.asarray(list(rs), dtype=float)
    n = len(a)
    if n == 0:
        return RStats(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    mean = float(a.mean())
    std = float(a.std(ddof=1)) if n > 1 else 0.0
    sqn = mean / std * math.sqrt(min(n, SQN_CAP)) if std > 1e-12 else (0.0 if mean == 0 else math.copysign(3.0, mean))
    eq = np.cumsum(a)
    peak = np.maximum.accumulate(np.r_[0.0, eq])[1:]
    return RStats(n, mean, std, sqn, float((a > 0).mean()), float(a.sum()), float((peak - eq).max(initial=0.0)))


def deflation(trials: int) -> float:
    """Expected best SQN among ``trials`` edgeless candidates ≈ sqrt(2 ln trials), scaled by DEFLATE_K."""
    return DEFLATE_K * math.sqrt(2 * math.log(max(trials, 1)))


@dataclass(frozen=True)
class WalkForward:
    in_sample: RStats
    oos: RStats
    folds: tuple[RStats, ...]  # the out-of-sample part split into consecutive folds
    oos_curve: tuple[tuple[int, float], ...]  # (close_time, cumulative R) for charts

    @property
    def positive_folds(self) -> int:
        return sum(1 for f in self.folds if f.n and f.mean > 0)

    def to_dict(self) -> dict:
        return {
            "in_sample": self.in_sample.to_dict(),
            "oos": self.oos.to_dict(),
            "folds": [f.to_dict() for f in self.folds],
            "positive_folds": self.positive_folds,
            "oos_curve": [list(p) for p in self.oos_curve],
        }


def walk_forward(trades: list[PaperTrade], split_time: int, fold_edges: list[int]) -> WalkForward:
    """Split one Backtest's trades by *open time*: before ``split_time`` is in-sample (used to rank
    Candidates), after it is out-of-sample, cut into folds at ``fold_edges``."""
    ins = [t.r for t in trades if t.open_time < split_time]
    oos_trades = [t for t in trades if t.open_time >= split_time]
    folds = []
    edges = [split_time, *fold_edges, 2**62]
    for a, b in zip(edges[:-1], edges[1:]):
        folds.append(r_stats(t.r for t in oos_trades if a <= t.open_time < b))
    cum, curve = 0.0, []
    for t in sorted(oos_trades, key=lambda t: t.close_time):
        cum += t.r
        curve.append((t.close_time, round(cum, 4)))
    return WalkForward(r_stats(ins), r_stats(t.r for t in oos_trades), tuple(folds), tuple(curve))


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    value: float | None
    threshold: float | None
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


def guardrails(
    challenger_shadow: RStats,
    champion_shadow: RStats,
    challenger_oos: RStats | None,
    champion_oos: RStats | None,
    trials: int,
    robust: bool | None,
) -> list[Check]:
    """Every condition a Challenger must meet to be promotable (ADR 0004). Shadow stats must cover
    the same bars (the champion's shadow trades since the challenger started)."""
    checks = [
        Check(
            "shadow_trades",
            challenger_shadow.n >= MIN_SHADOW_TRADES,
            challenger_shadow.n,
            MIN_SHADOW_TRADES,
            f"{challenger_shadow.n} of {MIN_SHADOW_TRADES} Shadow Trades",
        ),
        Check(
            "shadow_sqn",
            challenger_shadow.sqn >= SHADOW_MIN_SQN,
            round(challenger_shadow.sqn, 2),
            SHADOW_MIN_SQN,
            f"shadow SQN {challenger_shadow.sqn:.2f} (needs ≥ {SHADOW_MIN_SQN})",
        ),
        Check(
            "beats_champion",
            challenger_shadow.mean - champion_shadow.mean >= SHADOW_MIN_EDGE_R,
            round(challenger_shadow.mean - champion_shadow.mean, 3),
            SHADOW_MIN_EDGE_R,
            f"mean {challenger_shadow.mean:+.2f}R vs champion {champion_shadow.mean:+.2f}R on the same bars",
        ),
    ]
    if challenger_oos is None or champion_oos is None:
        checks.append(Check("walk_forward", False, None, None, "no Walk-forward result yet — run the optimizer"))
    else:
        # must beat the champion AND zero edge, by more than the best of `trials` noise candidates would
        need = max(champion_oos.sqn, 0.0) + deflation(trials)
        checks.append(
            Check(
                "walk_forward",
                challenger_oos.n >= MIN_OOS_TRADES and challenger_oos.sqn > need,
                round(challenger_oos.sqn, 2),
                round(need, 2),
                f"out-of-sample SQN {challenger_oos.sqn:.2f} over {challenger_oos.n} trades vs max(champion "
                f"{champion_oos.sqn:.2f}, 0) + selection penalty {deflation(trials):.2f}",
            )
        )
    checks.append(
        Check("robust", bool(robust), None, None, "neighbouring parameters also profitable" if robust else "edge not robust to small parameter changes (or not measured)")
    )
    dd_cap = MAX_DD_RATIO * champion_shadow.max_dd + 1.0
    checks.append(
        Check(
            "drawdown",
            challenger_shadow.max_dd <= dd_cap,
            round(challenger_shadow.max_dd, 2),
            round(dd_cap, 2),
            f"shadow drawdown {challenger_shadow.max_dd:.1f}R (cap {dd_cap:.1f}R)",
        )
    )
    return checks


def should_rollback(live_rs_since_promotion: list[float], previous_champion_mean_r: float) -> bool:
    """After ROLLBACK_TRADES live trades, roll back if the new Champion trails the old one's expectancy."""
    if len(live_rs_since_promotion) < ROLLBACK_TRADES:
        return False
    recent = float(np.mean(live_rs_since_promotion[-ROLLBACK_TRADES:]))
    return recent < previous_champion_mean_r - ROLLBACK_MARGIN_R
