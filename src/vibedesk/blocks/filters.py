"""Filter Blocks: each returns (allow_long, allow_short), combined with the entry by AND.

The value at bar t decides whether an entry signalled at bar t's close may be taken. Market
filters judge the market at that close. Time filters (`session_window`, `day_of_week`) judge
when the trade would open: the entry bar, the next bar's open (owner's decision, 2026-10-22).
That reads the next bar's open time, a fact of the clock and never a price.
"""

import re
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vibedesk.blocks.indicators import adx, atr, sma, trailing_percentile
from vibedesk.blocks.registry import LIBRARY, Kind
from vibedesk.market.bars import Bars
from vibedesk.market.server_time import server_weekdays
from vibedesk.market.sessions import local_minute_of_day

SLOPE_BARS = 10  # ma_slope measures the moving average's change over this many bars


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MaSlopeParams(_Params):
    period: int = Field(100, ge=2, le=1000)
    min_slope_atr: float = Field(0.0, ge=0, le=10)


@LIBRARY.register("ma_slope", Kind.FILTER, MaSlopeParams)
def ma_slope(bars: Bars, p: MaSlopeParams) -> tuple[np.ndarray, np.ndarray]:
    """Trade with the trend of a moving average.

    The slope is the SMA's change over the last 10 bars, in units of ATR(14). Longs are allowed
    while the slope is above `min_slope_atr`, shorts while it is below minus that. With 0 that
    means "the average is rising" or "falling".
    """
    average = sma(bars.close, p.period)
    change = np.full(len(bars), np.nan)
    change[SLOPE_BARS:] = average[SLOPE_BARS:] - average[:-SLOPE_BARS]
    with np.errstate(divide="ignore", invalid="ignore"):  # ATR of zero on frozen prices
        slope = change / atr(bars.high, bars.low, bars.close, 14)
    return slope > p.min_slope_atr, slope < -p.min_slope_atr


class AdxParams(_Params):
    period: int = Field(14, ge=2, le=100)
    min: float = Field(0.0, ge=0, le=100)
    max: float = Field(100.0, ge=0, le=100)

    @model_validator(mode="after")
    def _ordered(self):
        if self.min > self.max:
            raise ValueError("min must not be above max")
        return self


@LIBRARY.register("adx", Kind.FILTER, AdxParams)
def adx_filter(bars: Bars, p: AdxParams) -> tuple[np.ndarray, np.ndarray]:
    """Trade only while trend strength, Wilder's ADX(`period`), is between `min` and `max`.
    High ADX means a strong trend in either direction; low ADX means a range."""
    value = adx(bars.high, bars.low, bars.close, p.period)
    allowed = (value >= p.min) & (value <= p.max)
    return allowed, allowed.copy()


class AtrRegimeParams(_Params):
    period: int = Field(14, ge=2, le=200)
    lookback: int = Field(100, ge=10, le=2000)
    percentile_min: float = Field(0.0, ge=0, le=100)
    percentile_max: float = Field(100.0, ge=0, le=100)

    @model_validator(mode="after")
    def _ordered(self):
        if self.percentile_min > self.percentile_max:
            raise ValueError("percentile_min must not be above percentile_max")
        return self


@LIBRARY.register("atr_regime", Kind.FILTER, AtrRegimeParams)
def atr_regime(bars: Bars, p: AtrRegimeParams) -> tuple[np.ndarray, np.ndarray]:
    """Trade only in a chosen volatility regime: ATR(`period`)'s percentile among its own last
    `lookback` values (past bars only) must be between `percentile_min` and `percentile_max`."""
    rank = trailing_percentile(atr(bars.high, bars.low, bars.close, p.period), p.lookback)
    allowed = (rank >= p.percentile_min) & (rank <= p.percentile_max)
    return allowed, allowed.copy()


_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class SessionWindowParams(_Params):
    zone: Literal["london", "new_york", "tokyo"] = "london"
    start: str = "08:00"
    end: str = "16:00"

    @field_validator("start", "end")
    @classmethod
    def _clock_time(cls, value: str) -> str:
        if not _HHMM.match(value):
            raise ValueError("use 24-hour HH:MM")
        return value

    @model_validator(mode="after")
    def _not_empty(self):
        if self.start == self.end:
            raise ValueError("start and end must differ")
        return self


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def entry_times(bars: Bars) -> np.ndarray:
    """When an entry signalled at each bar would open: the next bar's open, so a Friday signal
    on D1 opens on Monday. The last bar has no next bar yet; its entry would never fill, so it
    gets one bar later on the clock."""
    if not len(bars):
        return bars.time.copy()
    return np.append(bars.time[1:], bars.time[-1] + bars.timeframe.seconds)


@LIBRARY.register("session_window", Kind.FILTER, SessionWindowParams)
def session_window(bars: Bars, p: SessionWindowParams) -> tuple[np.ndarray, np.ndarray]:
    """Trade only when the trade's entry bar opens inside a local-time window of the zone:
    [start, end). An end before the start wraps past midnight, for example tokyo 22:00 to
    02:00."""
    minute = local_minute_of_day(entry_times(bars), p.zone)
    start, end = _minutes(p.start), _minutes(p.end)
    if start < end:
        allowed = (minute >= start) & (minute < end)
    else:
        allowed = (minute >= start) | (minute < end)
    return allowed, allowed.copy()


class DayOfWeekParams(_Params):
    mon: bool = True
    tue: bool = True
    wed: bool = True
    thu: bool = True
    fri: bool = True

    @model_validator(mode="after")
    def _some_day(self):
        if not any((self.mon, self.tue, self.wed, self.thu, self.fri)):
            raise ValueError("allow at least one day")
        return self


@LIBRARY.register("day_of_week", Kind.FILTER, DayOfWeekParams)
def day_of_week(bars: Bars, p: DayOfWeekParams) -> tuple[np.ndarray, np.ndarray]:
    """Trade only when the trade's entry bar opens on one of the chosen server-clock weekdays
    (the broker's trading days)."""
    time = entry_times(bars)
    days = np.array([p.mon, p.tue, p.wed, p.thu, p.fri, False, False])
    allowed = days[server_weekdays(time)]
    return allowed, allowed.copy()
