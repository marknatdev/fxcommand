"""Plain pandas indicators. Wilder smoothing where the classic definition uses it."""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _wilder(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1 / period, adjust=False).mean()


def true_range(bars: pd.DataFrame) -> pd.Series:
    prev_close = bars["close"].shift(1)
    tr = pd.concat(
        [bars["high"] - bars["low"], (bars["high"] - prev_close).abs(), (bars["low"] - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr


def atr(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    return _wilder(true_range(bars), period)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = _wilder(delta.clip(lower=0), period)
    loss = _wilder(-delta.clip(upper=0), period)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = gain / loss  # inf when there were no losses -> RSI 100
        out = 100 - 100 / (1 + rs)
    return out.fillna(50.0)  # flat market (no gains, no losses)


def adx(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    up = bars["high"].diff()
    down = -bars["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=bars.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=bars.index)
    tr = _wilder(true_range(bars), period)
    plus_di = 100 * _wilder(plus_dm, period) / tr.replace(0, np.nan)
    minus_di = 100 * _wilder(minus_dm, period) / tr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return _wilder(dx.fillna(0.0), period)


def donchian(bars: pd.DataFrame, period: int) -> tuple[pd.Series, pd.Series]:
    """Upper/lower channel of the *previous* ``period`` bars (excludes the current bar)."""
    upper = bars["high"].rolling(period).max().shift(1)
    lower = bars["low"].rolling(period).min().shift(1)
    return upper, lower
