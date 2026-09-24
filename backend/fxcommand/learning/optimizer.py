"""Optimizer Run (pure): generate Candidates, Backtest them, rank on the in-sample part, then judge
the best few on the out-of-sample part with a robustness check.

Selection happens only on the in-sample bars; the out-of-sample bars are looked at for the
``top_k`` finalists alone, which is why the Walk-forward Guardrail deflates by ``top_k`` trials.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from .candidate import Candidate, generate, perturb
from .objective import MIN_OOS_TRADES, ROBUST_SHARE, WalkForward, walk_forward
from .paper import Costs, ExitRules, backtest

MIN_BARS = 500
IN_SAMPLE_SHARE = 0.6
N_FOLDS = 3
NEIGHBOURS = 4


@dataclass
class Scored:
    candidate: Candidate
    wf: WalkForward
    robust: bool | None = None
    neighbours_median_sqn: float | None = None

    @property
    def is_sqn(self) -> float:
        return self.wf.in_sample.sqn if self.wf.in_sample.n >= 20 else -99.0

    def to_dict(self) -> dict:
        return {
            "candidate": self.candidate.to_dict(),
            "walk_forward": self.wf.to_dict(),
            "robust": self.robust,
            "neighbours_median_sqn": self.neighbours_median_sqn,
        }


@dataclass
class OptimizeResult:
    champion: Scored | None
    finalists: list[Scored] = field(default_factory=list)  # top_k by in-sample, with OOS + robustness
    picks: list[Scored] = field(default_factory=list)  # finalists that qualify as Challengers, best first
    trials: int = 0
    evaluated: int = 0
    bars: int = 0
    split_time: int = 0
    insufficient: str | None = None

    def to_dict(self) -> dict:
        return {
            "champion": self.champion.to_dict() if self.champion else None,
            "finalists": [s.to_dict() for s in self.finalists],
            "picks": [s.candidate.key for s in self.picks],
            "trials": self.trials,
            "evaluated": self.evaluated,
            "bars": self.bars,
            "split_time": self.split_time,
            "insufficient": self.insufficient,
        }


def _score(bars, cand, costs, rules, allow, split, edges) -> Scored:
    return Scored(cand, walk_forward(backtest(bars, cand, costs, rules, allow), split, edges))


def optimize(
    bars: pd.DataFrame,
    champion: Candidate,
    costs: Costs,
    rules: ExitRules | None = None,
    n_candidates: int = 200,
    top_k: int = 10,
    seed: int = 0,
    allow_entry: Callable[[int], bool] | None = None,
    exclude: set[str] | None = None,
) -> OptimizeResult:
    bars = bars.reset_index(drop=True)
    if len(bars) < MIN_BARS:
        return OptimizeResult(None, bars=len(bars), insufficient=f"only {len(bars)} bars of history (needs {MIN_BARS})")
    rng = np.random.default_rng(seed)
    t = bars["time"].to_numpy(dtype=np.int64)
    split = int(t[int(len(t) * IN_SAMPLE_SHARE)])
    oos_idx = np.linspace(int(len(t) * IN_SAMPLE_SHARE), len(t) - 1, N_FOLDS + 1).astype(int)[1:-1]
    edges = [int(t[i]) for i in oos_idx]
    rules = rules or ExitRules()

    champ = _score(bars, champion, costs, rules, allow_entry, split, edges)
    cands = [c for c in generate(champion, n_candidates, rng) if not exclude or c.key not in exclude]
    scored = [_score(bars, c, costs, rules, allow_entry, split, edges) for c in cands]
    finalists = sorted(scored, key=lambda s: s.is_sqn, reverse=True)[:top_k]
    for s in finalists:
        neigh = [_score(bars, perturb(s.candidate, rng, 0.12), costs, rules, allow_entry, split, edges) for _ in range(NEIGHBOURS)]
        med = statistics.median(n.wf.oos.sqn for n in neigh)
        s.neighbours_median_sqn = round(med, 3)
        s.robust = s.wf.oos.sqn > 0 and med >= ROBUST_SHARE * s.wf.oos.sqn
    picks = sorted(
        (s for s in finalists if s.robust and s.wf.oos.n >= MIN_OOS_TRADES and s.wf.oos.sqn > 0 and s.wf.positive_folds >= 2),
        key=lambda s: s.wf.oos.sqn,
        reverse=True,
    )
    return OptimizeResult(
        champion=champ,
        finalists=finalists,
        picks=picks,
        trials=top_k,
        evaluated=len(scored) + 1 + NEIGHBOURS * len(finalists),
        bars=len(bars),
        split_time=split,
    )


def optimize_job(
    bars: pd.DataFrame,
    champion: Candidate,
    costs: Costs,
    rules: ExitRules,
    n_candidates: int,
    seed: int,
    window: dict | None,
    bar_seconds: int,
    exclude: set[str],
) -> OptimizeResult:
    """Picklable entry point for running an Optimizer Run in a separate process (no lambdas)."""
    from ..risk import TradingWindow

    allow = None
    if window is not None:
        w = TradingWindow.from_dict(window)
        allow = lambda ts: w.is_open(ts + bar_seconds)  # noqa: E731
    return optimize(bars, champion, costs, rules, n_candidates=n_candidates, seed=seed, allow_entry=allow, exclude=exclude)
