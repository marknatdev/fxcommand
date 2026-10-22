"""Indicator primitives. Value t uses only bars at or before t; NaN until enough history exists."""

import numpy as np
from numba import njit
from numpy.lib.stride_tricks import sliding_window_view


def _f64(values) -> np.ndarray:
    return np.asarray(values, dtype=np.float64)


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    """Wilder's Average True Range, first defined at index period - 1."""
    return _wilder_atr(_f64(high), _f64(low), _f64(close), period)


def sma(values: np.ndarray, period: int) -> np.ndarray:
    """Simple moving average of the last `period` values including t."""
    return _sma(_f64(values), period)


def ema(values: np.ndarray, period: int) -> np.ndarray:
    """Exponential moving average, seeded with the SMA of the first `period` values."""
    return _ema(_f64(values), period)


def moving_average(values: np.ndarray, period: int, kind: str) -> np.ndarray:
    return ema(values, period) if kind == "ema" else sma(values, period)


def rolling_std(values: np.ndarray, period: int) -> np.ndarray:
    """Population standard deviation of the last `period` values including t."""
    return _rolling_std(_f64(values), period)


def rsi(close: np.ndarray, period: int) -> np.ndarray:
    """Wilder's Relative Strength Index, first defined at index `period`."""
    return _wilder_rsi(_f64(close), period)


def rolling_max_prev(values: np.ndarray, period: int) -> np.ndarray:
    """Highest value of the `period` bars before t, excluding t itself."""
    return _rolling_prev(values, period, np.max)


def rolling_min_prev(values: np.ndarray, period: int) -> np.ndarray:
    """Lowest value of the `period` bars before t, excluding t itself."""
    return _rolling_prev(values, period, np.min)


def crossed_above(series: np.ndarray, level: np.ndarray | float) -> np.ndarray:
    """True at t when series[t] > level[t] and series[t-1] <= level[t-1]."""
    series = _f64(series)
    level = np.broadcast_to(_f64(level), series.shape)
    out = np.zeros(len(series), dtype=np.bool_)
    out[1:] = (series[1:] > level[1:]) & (series[:-1] <= level[:-1])
    return out


def crossed_below(series: np.ndarray, level: np.ndarray | float) -> np.ndarray:
    """True at t when series[t] < level[t] and series[t-1] >= level[t-1]."""
    series = _f64(series)
    level = np.broadcast_to(_f64(level), series.shape)
    out = np.zeros(len(series), dtype=np.bool_)
    out[1:] = (series[1:] < level[1:]) & (series[:-1] >= level[:-1])
    return out


def _rolling_prev(values: np.ndarray, period: int, reduce) -> np.ndarray:
    values = _f64(values)
    out = np.full(len(values), np.nan)
    if len(values) > period:
        out[period:] = reduce(sliding_window_view(values, period), axis=1)[:-1]
    return out


@njit(cache=True)
def _wilder_atr(high, low, close, period):
    n = len(high)
    out = np.full(n, np.nan)
    if n < period:
        return out
    true_range = np.empty(n)
    true_range[0] = high[0] - low[0]
    for i in range(1, n):
        true_range[i] = max(
            high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])
        )
    value = true_range[:period].mean()
    out[period - 1] = value
    for i in range(period, n):
        value = (value * (period - 1) + true_range[i]) / period
        out[i] = value
    return out


@njit(cache=True)
def _sma(values, period):
    n = len(values)
    out = np.full(n, np.nan)
    for i in range(period - 1, n):
        out[i] = values[i - period + 1 : i + 1].mean()
    return out


@njit(cache=True)
def _ema(values, period):
    n = len(values)
    out = np.full(n, np.nan)
    if n < period:
        return out
    alpha = 2.0 / (period + 1)
    value = values[:period].mean()
    out[period - 1] = value
    for i in range(period, n):
        value = alpha * values[i] + (1 - alpha) * value
        out[i] = value
    return out


@njit(cache=True)
def _rolling_std(values, period):
    n = len(values)
    out = np.full(n, np.nan)
    for i in range(period - 1, n):
        out[i] = values[i - period + 1 : i + 1].std()
    return out


@njit(cache=True)
def _wilder_rsi(close, period):
    n = len(close)
    out = np.full(n, np.nan)
    if n <= period:
        return out
    gain = 0.0
    loss = 0.0
    for i in range(1, period + 1):
        change = close[i] - close[i - 1]
        gain += max(change, 0.0)
        loss += max(-change, 0.0)
    gain /= period
    loss /= period
    for i in range(period, n):
        if i > period:
            change = close[i] - close[i - 1]
            gain = (gain * (period - 1) + max(change, 0.0)) / period
            loss = (loss * (period - 1) + max(-change, 0.0)) / period
        if loss == 0:
            out[i] = 50.0 if gain == 0 else 100.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + gain / loss)
    return out


def adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    """Wilder's Average Directional Index, first defined at index 2 * period - 1."""
    return _wilder_adx(_f64(high), _f64(low), _f64(close), period)


def trailing_percentile(values: np.ndarray, lookback: int) -> np.ndarray:
    """Mid-rank percentile (0-100) of values[t] among the last `lookback` values including t.

    Ties count half, so a constant series sits at 50. NaN until the window is full of numbers.
    """
    return _trailing_percentile(_f64(values), lookback)


@njit(cache=True)
def _wilder_adx(high, low, close, period):
    n = len(high)
    out = np.full(n, np.nan)
    if n < 2 * period:
        return out
    tr = np.zeros(n)
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
        up = high[i] - high[i - 1]
        down = low[i - 1] - low[i]
        if up > down and up > 0:
            plus_dm[i] = up
        if down > up and down > 0:
            minus_dm[i] = down
    s_tr = tr[1 : period + 1].sum()
    s_plus = plus_dm[1 : period + 1].sum()
    s_minus = minus_dm[1 : period + 1].sum()
    dx = np.full(n, np.nan)
    for i in range(period, n):
        if i > period:
            s_tr = s_tr - s_tr / period + tr[i]
            s_plus = s_plus - s_plus / period + plus_dm[i]
            s_minus = s_minus - s_minus / period + minus_dm[i]
        if s_tr == 0:
            dx[i] = 0.0
            continue
        plus_di = 100.0 * s_plus / s_tr
        minus_di = 100.0 * s_minus / s_tr
        total = plus_di + minus_di
        dx[i] = 0.0 if total == 0 else 100.0 * abs(plus_di - minus_di) / total
    value = dx[period : 2 * period].mean()
    out[2 * period - 1] = value
    for i in range(2 * period, n):
        value = (value * (period - 1) + dx[i]) / period
        out[i] = value
    return out


@njit(cache=True)
def _trailing_percentile(values, lookback):
    n = len(values)
    out = np.full(n, np.nan)
    for i in range(lookback - 1, n):
        current = values[i]
        if np.isnan(current):
            continue
        below = 0.0
        equal = 0.0
        full = True
        for j in range(i - lookback + 1, i + 1):
            v = values[j]
            if np.isnan(v):
                full = False
                break
            if v < current:
                below += 1
            elif v == current:
                equal += 1
        if full:
            out[i] = 100.0 * (below + 0.5 * equal) / lookback
    return out
