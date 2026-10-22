"""The Pass Bar (ADR 0007, spec §6.3): every check, the Verdict and the Closest Miss.

A Trial is a CANDIDATE only if no check fails. A check whose question has no answer (PBO for a
grid of one, a Prosecutor split with too few trades) is "n/a": it neither passes nor fails. The
core checks can never be n/a; a value that can't be computed fails them. Otherwise the Verdict
is REJECT, and the Closest Miss is the failed check that came nearest to its threshold. It is
chosen among the failed core checks first: a record that fails its profit factor has not nearly
passed because one Prosecutor split came close. A Prosecutor flag is the Closest Miss only when
every core check passed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass

PASS, FAIL, NA = "pass", "fail", "n/a"
CANDIDATE, REJECT = "CANDIDATE", "REJECT"

MIN_PROFIT_FACTOR = 1.2
MIN_TRADES = 100
POSITIVE_FOLD_SHARE = 5 / 8  # at least 5 of 8 folds
MIN_STRESS_PROFIT_FACTOR = 1.05
BASELINE_PERCENTILE = 0.95
MIN_DEFLATED_SHARPE = 0.95
MAX_PBO = 0.3
CORE = (
    "profit_factor",
    "trades",
    "positive_folds",
    "stress_profit_factor",
    "coin_flip_baseline",
    "deflated_sharpe",
    "pbo",
)


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    value: float  # NaN when it could not be computed
    threshold: float
    at_most: bool  # True when the value must stay at or below the threshold
    status: str  # PASS, FAIL or NA
    note: str = ""

    def closeness(self) -> float:
        """How near a failed value came to passing: 1 at the threshold, smaller further away."""
        if math.isnan(self.value):
            return -math.inf
        if self.at_most:
            return self.threshold / self.value if self.value > 0 else math.inf
        return self.value / self.threshold if self.threshold else -math.inf

    def to_dict(self) -> dict:
        return {
            k: (None if isinstance(v, float) and math.isnan(v) else v)
            for k, v in asdict(self).items()
        }


def judged(
    name: str,
    value: float,
    threshold: float,
    *,
    at_most: bool = False,
    na_note: str | None = None,
    note: str = "",
) -> Check:
    """A check of `value` against `threshold`.

    NaN fails, unless `na_note` says why the check doesn't apply; then it is n/a with that note.
    """
    value = float(value)
    if math.isnan(value):
        if na_note is None:
            return Check(name, value, threshold, at_most, FAIL, note)
        return Check(name, value, threshold, at_most, NA, na_note)
    passed = value <= threshold if at_most else value >= threshold
    return Check(name, value, threshold, at_most, PASS if passed else FAIL, note)


def not_applicable(name: str, threshold: float, note: str, *, at_most: bool = False) -> Check:
    return Check(name, math.nan, threshold, at_most, NA, note)


def pass_bar(wf, baseline, deflated_sharpe: float, pbo: float) -> list[Check]:
    """The core checks of a walk-forward. The Prosecutor's checks are added by the caller.

    `wf` is a WalkForward, `baseline` a Baseline (or anything with the same fields).
    """
    folds = len(wf.fold_net_r)
    needed = math.ceil(folds * POSITIVE_FOLD_SHARE)
    positive = int((wf.fold_net_r > 0).sum())
    beat = baseline.strategy_net_r > baseline.p95
    return [
        judged("profit_factor", wf.metrics.profit_factor, MIN_PROFIT_FACTOR),
        judged("trades", wf.metrics.trades, MIN_TRADES),
        judged("positive_folds", positive, needed, note=f"{positive} of {folds} folds"),
        judged("stress_profit_factor", wf.stress_metrics.profit_factor, MIN_STRESS_PROFIT_FACTOR),
        Check(
            "coin_flip_baseline",
            float(baseline.percentile),
            BASELINE_PERCENTILE,
            False,
            PASS if beat else FAIL,
            f"net R {baseline.strategy_net_r:.2f} against a 95th percentile of {baseline.p95:.2f}",
        ),
        judged("deflated_sharpe", deflated_sharpe, MIN_DEFLATED_SHARPE),
        judged("pbo", pbo, MAX_PBO, at_most=True, na_note="a grid of one has nothing to overfit"),
    ]


@dataclass(frozen=True, slots=True)
class Verdict:
    verdict: str  # CANDIDATE or REJECT
    checks: tuple[Check, ...]
    closest_miss: dict | None  # {"check", "value", "threshold"} for a REJECT

    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]


def decide(checks: Sequence[Check]) -> Verdict:
    failed = [c for c in checks if c.status == FAIL]
    if not failed:
        return Verdict(CANDIDATE, tuple(checks), None)
    core = [c for c in failed if c.name in CORE]
    # The ratio nearest 1; ties keep the first listed check. A failed check's ratio is below 1,
    # except the baseline at a tie with its 95th percentile, which is as near as it gets.
    nearest = max(core or failed, key=lambda c: min(c.closeness(), 1.0))
    miss = {
        "check": nearest.name,
        "value": None if math.isnan(nearest.value) else nearest.value,
        "threshold": nearest.threshold,
    }
    return Verdict(REJECT, tuple(checks), miss)
