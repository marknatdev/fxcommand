"""The Pass Bar's statistics: Sharpe, the Deflated Sharpe and PBO (backtest overfitting).

Every Sharpe here is the non-annualised Sharpe of daily R, so the Deflated Sharpe's inputs (the
blind-year Sharpe, its T and the variance of the grid's Sharpe ratios) are all on one scale.
Conventions:
- A series shorter than 2 days has no sample std: its Sharpe is NaN.
- A flat series (variance zero up to rounding) has a Sharpe of 0, so no-trade days, blocks or
  combinations never look like an edge, and a degenerate series can never pass.
"""

from __future__ import annotations

import math
from functools import cache
from itertools import combinations
from statistics import NormalDist

import numpy as np

EULER_GAMMA = 0.5772156649015329  # Euler–Mascheroni constant
_FLAT = 1e-12  # a variance below this share of the mean square is rounding, not risk
_NORMAL = NormalDist()


def sharpe(daily_r) -> float:
    """Mean over sample std (ddof = 1), not annualised. NaN under 2 days, 0 for a flat series."""
    x = _series(daily_r)
    if len(x) < 2:
        return math.nan
    return float(_sharpe_from_sums(x.sum(), (x**2).sum(), len(x)))


def probabilistic_sharpe(
    sr: float, benchmark: float, t: int, skew: float, kurtosis: float
) -> float:
    """Probability that the true Sharpe beats `benchmark`, given Sharpe `sr` over `t` days.

    `kurtosis` is non-excess (3 for a normal). NaN when t < 2 or the variance term is not
    positive, which no real sample gives (sample moments satisfy kurtosis >= 1 + skew²).
    """
    spread = 1 - skew * sr + (kurtosis - 1) / 4 * sr**2
    if t < 2 or not spread > 0:
        return math.nan
    return _NORMAL.cdf((sr - benchmark) * math.sqrt(t - 1) / math.sqrt(spread))


def expected_max_sharpe(n_trials: int, var_trial_sharpe: float) -> float:
    """SR₀: the best Sharpe that `n_trials` skill-less combinations are expected to show by luck.

    Bailey & López de Prado (2014). With one trial, or no spread between trials, there is no
    luck to deflate and SR₀ is 0.
    """
    if n_trials < 1:
        raise ValueError(f"n_trials must be at least 1, got {n_trials}")
    if not (math.isfinite(var_trial_sharpe) and var_trial_sharpe >= 0):
        raise ValueError(f"var_trial_sharpe must be finite and >= 0, got {var_trial_sharpe}")
    if n_trials == 1 or var_trial_sharpe == 0:
        return 0.0
    quantile = (1 - EULER_GAMMA) * _NORMAL.inv_cdf(1 - 1 / n_trials)
    quantile += EULER_GAMMA * _NORMAL.inv_cdf(1 - 1 / (n_trials * math.e))
    return math.sqrt(var_trial_sharpe) * quantile


def deflated_sharpe(daily_r, n_trials: int, var_trial_sharpe: float) -> float:
    """Probability that the Sharpe of `daily_r` beats the best a grid search finds by luck.

    In the Gauntlet `daily_r` is the stitched blind years' daily R, zero days included;
    `n_trials` is every grid combination evaluated across the Idea Family's Trials (ADR 0003);
    `var_trial_sharpe` is the variance of those combinations' blind-year daily Sharpe ratios.
    Skew and non-excess kurtosis are the population moments of `daily_r`. NaN under 2 days.
    """
    x = _series(daily_r)
    benchmark = expected_max_sharpe(n_trials, var_trial_sharpe)
    if len(x) < 2:
        return math.nan
    sr = sharpe(x)
    # Skew and kurtosis only enter multiplied by SR; a flat series has no moments to give.
    skew, kurtosis = (0.0, 3.0) if sr == 0 else _shape(x)
    return probabilistic_sharpe(sr, benchmark, len(x), skew, kurtosis)


def pbo(returns, blocks: int = 16) -> float:
    """Probability of backtest overfitting by CSCV (Bailey, Borwein, López de Prado & Zhu).

    `returns` is days × grid combinations of daily R. The days are cut into `blocks` contiguous
    blocks, as equal as possible. For every choice of half the blocks as in-sample (all 12,870
    for 16), the in-sample best combination's out-of-sample Sharpe is ranked among all
    combinations': rank 1 is the worst, ties share the average rank, and ω = rank / (N + 1).
    PBO is the share of splits with logit(ω) <= 0, an in-sample best that lands at or below the
    out-of-sample median. In-sample ties go to the first column. One combination gives NaN
    ("n/a"): there was no selection to overfit.
    """
    m = np.asarray(returns, dtype=np.float64)
    if m.ndim != 2 or m.shape[1] < 1:
        raise ValueError(f"returns must be days × combinations, got shape {m.shape}")
    if blocks < 2 or blocks % 2:
        raise ValueError(f"blocks must be even and at least 2, got {blocks}")
    if len(m) < 2 * blocks:
        raise ValueError(f"{len(m)} days is too few for {blocks} blocks of at least 2 days")
    if not np.isfinite(m).all():
        raise ValueError("returns must be finite")
    n = m.shape[1]
    if n == 1:
        return math.nan

    sizes = np.full(blocks, len(m) // blocks)
    sizes[: len(m) % blocks] += 1  # the split np.array_split makes
    starts = np.r_[0, np.cumsum(sizes)[:-1]]
    block_sum = np.add.reduceat(m, starts, axis=0)
    block_sq = np.add.reduceat(m**2, starts, axis=0)

    train = _halves(blocks)
    test = 1.0 - train
    train_sr = _sharpe_from_sums(train @ block_sum, train @ block_sq, (train @ sizes)[:, None])
    test_sr = _sharpe_from_sums(test @ block_sum, test @ block_sq, (test @ sizes)[:, None])

    best = np.argmax(train_sr, axis=1)
    picked = test_sr[np.arange(len(best)), best][:, None]
    below = (test_sr < picked).sum(axis=1)
    level = (test_sr == picked).sum(axis=1)  # includes the pick itself
    omega = (below + (level + 1) / 2) / (n + 1)
    logit = np.log(omega / (1 - omega))
    return float(np.mean(logit <= 0))


def _series(daily_r) -> np.ndarray:
    x = np.asarray(daily_r, dtype=np.float64)
    if x.ndim != 1:
        raise ValueError(f"daily R must be one-dimensional, got shape {x.shape}")
    if not np.isfinite(x).all():
        raise ValueError("daily R must be finite")
    return x


def _sharpe_from_sums(s, q, n):
    """Sharpe (ddof = 1) from sums, sums of squares and counts, so unions of blocks are cheap."""
    mean = s / n
    var = (q - s * mean) / (n - 1)
    flat = var <= _FLAT * q / n
    return np.where(flat, 0.0, mean / np.sqrt(np.where(flat, 1.0, var)))


def _shape(x: np.ndarray) -> tuple[float, float]:
    """Population skewness and non-excess kurtosis."""
    d = x - x.mean()
    m2 = np.mean(d**2)
    return float(np.mean(d**3) / m2**1.5), float(np.mean(d**4) / m2**2)


@cache
def _halves(blocks: int) -> np.ndarray:
    """Every choice of half the blocks, one 0/1 row per split."""
    chosen = np.array(list(combinations(range(blocks), blocks // 2)))
    rows = np.zeros((len(chosen), blocks))
    rows[np.arange(len(chosen))[:, None], chosen] = 1.0
    rows.flags.writeable = False
    return rows
