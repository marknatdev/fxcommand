"""Signal Filter: logistic regression (pure numpy) estimating P(win) for a Signal from market features.

Lifecycle (ADR 0004):
- ``observe``   fewer than MIN_SAMPLES closed trades — scores Signals, never blocks.
- ``no_edge``   enough data, but blocking low scores did not improve held-out expectancy
                convincingly — keeps observing.
- ``active``    blocks Signals whose P(win) is below the threshold chosen on held-out trades.
- ``disabled``  switched itself off because recently blocked Signals did better than taken ones.
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field

import numpy as np

MIN_SAMPLES = 50
MIN_HOLDOUT = 15
MIN_LIFT_R = 0.05
LIFT_SE_MULT = 2.5  # held-out lift must exceed this many standard errors (≈0.6% false positives on noise)
MIN_KEEP_SHARE = 0.5
DISABLE_WINDOW = 40
DISABLE_MIN_BLOCKED = 10
L2 = 1.0


@dataclass
class FilterState:
    mode: str = "observe"
    samples: int = 0
    weights: list[float] = field(default_factory=list)
    bias: float = 0.0
    mean: list[float] = field(default_factory=list)
    std: list[float] = field(default_factory=list)
    threshold: float = 0.0
    lift_r: float = 0.0
    keep_share: float = 1.0
    holdout: int = 0
    updated: float = 0.0
    note: str = "collecting trades"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "FilterState":
        return cls(**d) if d else cls()


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def _fit(X: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, float, np.ndarray, np.ndarray]:
    mean, std = X.mean(axis=0), X.std(axis=0)
    std[std < 1e-9] = 1.0
    Z = np.hstack([(X - mean) / std, np.ones((len(X), 1))])  # last column = bias
    n, d = Z.shape
    reg = np.full(d, L2 / n)
    reg[-1] = 0.0  # bias is not regularised
    w = np.zeros(d)
    # Newton-Raphson on L2-regularised log-loss (small d: converges in a few steps)
    for _ in range(30):
        p = _sigmoid(Z @ w)
        g = Z.T @ (p - y) / n + reg * w
        H = (Z.T * (p * (1 - p))) @ Z / n + np.diag(reg) + 1e-6 * np.eye(d)
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-7:
            break
    w, b = w[:-1], float(w[-1])
    return w, b, mean, std


def predict(state: FilterState, features: list[float]) -> float | None:
    if not state.weights:
        return None
    z = (np.asarray(features) - np.asarray(state.mean)) / np.asarray(state.std)
    return float(_sigmoid(np.asarray([z @ np.asarray(state.weights) + state.bias]))[0])


def blocks(state: FilterState, p: float | None) -> bool:
    return state.mode == "active" and p is not None and p < state.threshold


def train(samples: list[tuple[list[float], float]], previous: FilterState | None = None) -> FilterState:
    """``samples``: (features, R) in chronological order. Keeps a ``disabled`` filter disabled."""
    n = len(samples)
    base = FilterState(samples=n, updated=time.time())
    if previous and previous.mode == "disabled":
        base.mode, base.note = "disabled", previous.note
    if n < MIN_SAMPLES:
        if base.mode != "disabled":
            base.note = f"observing: {n}/{MIN_SAMPLES} trades"
        return base
    X = np.asarray([s[0] for s in samples], dtype=float)
    r = np.asarray([s[1] for s in samples], dtype=float)
    y = (r > 0).astype(float)
    if y.min() == y.max():
        base.note = "all trades had the same outcome"
        return base

    # the threshold is chosen on the training part only; the held-out part is looked at ONCE,
    # so its lift is an honest estimate (no selection among thresholds on held-out data)
    cut = int(n * 0.7)
    Xtr, ytr, rtr, Xho, rho = X[:cut], y[:cut], r[:cut], X[cut:], r[cut:]
    w, b, mean, std = _fit(Xtr, ytr) if 0 < ytr.mean() < 1 else _fit(X, y)
    p_tr = _sigmoid(((Xtr - mean) / std) @ w + b)
    thr, best_train, improved = float(p_tr.min()) - 1e-9, float(rtr.mean()), False
    for q in np.linspace(0.1, 1 - MIN_KEEP_SHARE, 9):
        cand = float(np.quantile(p_tr, q))
        keep = p_tr >= cand
        if keep.mean() >= MIN_KEEP_SHARE and keep.any() and float(rtr[keep].mean()) > best_train:
            thr, best_train, improved = cand, float(rtr[keep].mean()), True
    p_ho = _sigmoid(((Xho - mean) / std) @ w + b)
    keep_ho = p_ho >= thr
    k = float(keep_ho.mean()) if len(rho) else 1.0
    all_mean = float(rho.mean()) if len(rho) else 0.0
    lift = float(rho[keep_ho].mean()) - all_mean if keep_ho.any() and k < 1.0 else 0.0
    keep_share = k
    sd = float(rho.std(ddof=1)) if len(rho) > 1 else float("inf")
    # standard error of (mean of kept − mean of all) when a share k of n trades is kept
    se = sd * math.sqrt(max(1 - k, 1e-9) / max(k * len(rho), 1e-9)) if 0 < k < 1 else float("inf")
    needed = max(MIN_LIFT_R, LIFT_SE_MULT * se)

    # final model on everything
    w, b, mean, std = _fit(X, y)
    base.weights, base.bias, base.mean, base.std = w.tolist(), float(b), mean.tolist(), std.tolist()
    base.lift_r, base.keep_share, base.holdout = round(lift, 4), round(keep_share, 3), len(rho)
    if base.mode == "disabled":
        return base
    if not improved:
        base.mode = "no_edge"
        base.note = "no edge: skipping low-scored signals did not improve even the training trades"
        return base
    if len(rho) < MIN_HOLDOUT or lift < needed or not math.isfinite(needed):
        base.mode = "no_edge"
        need_txt = f"{needed:.2f}R" if math.isfinite(needed) else "a measurable lift"
        base.note = f"no convincing edge: held-out lift {lift:+.2f}R over {len(rho)} trades (needs ≥ {need_txt})"
        return base
    base.mode, base.threshold = "active", round(thr, 4)
    base.note = f"blocking P(win) < {thr:.2f}: held-out lift {lift:+.2f}R keeping {keep_share:.0%} of signals"
    return base


def review(state: FilterState, recent: list[tuple[float, float]]) -> FilterState:
    """``recent``: (p_win, R) of the Champion's latest Shadow Trades (unfiltered). If the ones the
    filter would have blocked did better than the ones it keeps, switch the filter off."""
    if state.mode != "active":
        return state
    window = recent[-DISABLE_WINDOW:]
    blocked = [r for p, r in window if p < state.threshold]
    kept = [r for p, r in window if p >= state.threshold]
    if len(blocked) >= DISABLE_MIN_BLOCKED and kept and np.mean(blocked) > np.mean(kept):
        d = state.to_dict()
        d.update(
            mode="disabled",
            note=f"disabled: blocked signals averaged {np.mean(blocked):+.2f}R vs kept {np.mean(kept):+.2f}R",
            updated=time.time(),
        )
        return FilterState(**d)
    return state
