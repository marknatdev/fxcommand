"""Indicator primitives. Value t uses only bars at or before t; NaN until enough history exists."""

import numpy as np
from numba import njit
from numpy.lib.stride_tricks import sliding_window_view


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    """Wilder's Average True Range, first defined at index period - 1."""
    return _wilder_atr(
        np.asarray(high, dtype=np.float64),
        np.asarray(low, dtype=np.float64),
        np.asarray(close, dtype=np.float64),
        period,
    )


def rolling_max_prev(values: np.ndarray, period: int) -> np.ndarray:
    """Highest value of the `period` bars before t, excluding t itself."""
    return _rolling_prev(values, period, np.max)


def rolling_min_prev(values: np.ndarray, period: int) -> np.ndarray:
    """Lowest value of the `period` bars before t, excluding t itself."""
    return _rolling_prev(values, period, np.min)


def _rolling_prev(values: np.ndarray, period: int, reduce) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
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
