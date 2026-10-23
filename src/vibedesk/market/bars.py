"""Immutable price bars: the one shape every module above the market seam works with."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

import numpy as np


class Timeframe(StrEnum):
    M1 = "M1"
    M15 = "M15"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"

    @property
    def seconds(self) -> int:
        return _SECONDS[self]


_SECONDS = {
    Timeframe.M1: 60,
    Timeframe.M15: 900,
    Timeframe.H1: 3600,
    Timeframe.H4: 14400,
    Timeframe.D1: 86400,
}

_ARRAYS = {
    "time": np.int64,
    "open": np.float64,
    "high": np.float64,
    "low": np.float64,
    "close": np.float64,
    "spread": np.int32,
}


@dataclass(frozen=True, slots=True, eq=False)
class Bars:
    """Bid-price OHLC bars stamped with their UTC open time.

    `spread` is the minimum spread seen inside each bar, in points, because that is what MT5
    records. It understates real cost, which is why the Cost Model applies a Spread Floor.
    """

    symbol: str
    timeframe: Timeframe
    point: float
    digits: int
    time: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    spread: np.ndarray

    def __post_init__(self) -> None:
        if self.point <= 0:
            raise ValueError("point must be positive")
        arrays = {
            name: np.array(getattr(self, name), dtype=dtype) for name, dtype in _ARRAYS.items()
        }
        n = len(arrays["time"])
        for name, array in arrays.items():
            if array.ndim != 1 or len(array) != n:
                raise ValueError(f"{name} must be one-dimensional with the same length as time")
        if n > 1 and np.any(np.diff(arrays["time"]) <= 0):
            raise ValueError("bar times must be strictly increasing")
        # NaN passes every comparison below, so prices are checked on their own first.
        for name in ("open", "high", "low", "close"):
            if not np.all(np.isfinite(arrays[name]) & (arrays[name] > 0)):
                raise ValueError(f"{name} prices must be finite and positive")
        body_top = np.maximum(arrays["open"], arrays["close"])
        body_bottom = np.minimum(arrays["open"], arrays["close"])
        if np.any(arrays["high"] < body_top):
            raise ValueError("high must be at or above open and close")
        if np.any(arrays["low"] > body_bottom):
            raise ValueError("low must be at or below open and close")
        if np.any(arrays["spread"] < 0):
            raise ValueError("spread must not be negative")
        for name, array in arrays.items():
            array.setflags(write=False)
            object.__setattr__(self, name, array)

    def __len__(self) -> int:
        return len(self.time)

    def between(self, start: int, end: int) -> Bars:
        """Bars whose open time is in [start, end), as UTC epoch seconds."""
        lo, hi = np.searchsorted(self.time, [start, end])
        return replace(self, **{name: getattr(self, name)[lo:hi] for name in _ARRAYS})
