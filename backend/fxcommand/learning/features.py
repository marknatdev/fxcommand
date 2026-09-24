"""Market features describing the moment a Signal fires, for the Signal Filter.

Computed vectorised over bars (only past data up to each bar), then turned into a
vector for a given side at entry time. Directional features are signed by side so
"price above EMA50" means "with the trade" for longs and shorts alike.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..strategies import indicators as ind

FEATURE_NAMES = (
    "side",
    "spread_atr",
    "atr_rank",
    "adx",
    "ema50_dist",
    "ret10",
    "rsi",
    "hour_sin",
    "hour_cos",
    "wd_sin",
    "wd_cos",
    "prev_r",
)


def _rolling_rank(x: np.ndarray, window: int = 100, min_periods: int = 20) -> np.ndarray:
    """Share of the last ``window`` values that are <= the current one (percentile rank, past data only)."""
    n = len(x)
    out = np.full(n, np.nan)
    if n == 0:
        return out
    pad = np.concatenate([np.full(window - 1, np.nan), x])
    win = np.lib.stride_tricks.sliding_window_view(pad, window)  # row i = values up to and including x[i]
    valid = ~np.isnan(win)
    counts = valid.sum(axis=1)
    le = ((win <= x[:, None]) & valid).sum(axis=1)
    ok = (counts >= min_periods) & ~np.isnan(x)
    out[ok] = le[ok] / counts[ok]
    return out


def market_frame(bars: pd.DataFrame) -> pd.DataFrame:
    close = bars["close"]
    atr = ind.atr(bars, 14)
    atr_safe = atr.replace(0, np.nan)
    t = bars["time"].astype("int64")
    hour = (t % 86400) / 3600.0
    wd = (t // 86400 + 3) % 7
    return pd.DataFrame(
        {
            "atr": atr,
            "atr_rank": _rolling_rank(atr.to_numpy(dtype=float)),
            "adx": ind.adx(bars, 14) / 100.0,
            "ema50_dist": ((close - ind.ema(close, 50)) / atr_safe).clip(-10, 10),
            "ret10": ((close - close.shift(10)) / atr_safe).clip(-10, 10),
            "rsi": (ind.rsi(close, 14) - 50) / 50.0,
            "hour_sin": np.sin(2 * math.pi * hour / 24),
            "hour_cos": np.cos(2 * math.pi * hour / 24),
            "wd_sin": np.sin(2 * math.pi * wd / 7),
            "wd_cos": np.cos(2 * math.pi * wd / 7),
        },
        index=bars.index,
    )


def vector(row: pd.Series | dict, side: str, spread: float, prev_r: float) -> list[float]:
    s = 1.0 if side == "long" else -1.0
    atr = float(row["atr"]) or float("nan")

    def f(v) -> float:
        v = float(v)
        return 0.0 if v != v else v

    return [
        s,
        f(min(spread / atr, 5.0)) if atr == atr else 0.0,
        f(row["atr_rank"]),
        f(row["adx"]),
        s * f(row["ema50_dist"]),
        s * f(row["ret10"]),
        s * f(row["rsi"]),
        f(row["hour_sin"]),
        f(row["hour_cos"]),
        f(row["wd_sin"]),
        f(row["wd_cos"]),
        float(np.clip(prev_r, -3, 3)),
    ]
