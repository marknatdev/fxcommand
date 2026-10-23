"""Entry Blocks: each returns (long, short) boolean arrays of signals read at bar close.

Entries fire on events (a cross, a first close outside a band), not on states, so a condition
that stays true doesn't re-enter on every bar after an exit.
"""

from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from vibedesk.blocks.indicators import (
    atr,
    crossed_above,
    crossed_below,
    moving_average,
    rolling_max_prev,
    rolling_min_prev,
    rolling_std,
    rsi,
    sma,
)
from vibedesk.blocks.registry import LIBRARY, Kind
from vibedesk.market.bars import Bars
from vibedesk.market.sessions import session_opens


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DonchianBreakoutParams(_Params):
    period: int = Field(20, ge=2, le=500)


@LIBRARY.register("donchian_breakout", Kind.ENTRY, DonchianBreakoutParams)
def donchian_breakout(bars: Bars, p: DonchianBreakoutParams) -> tuple[np.ndarray, np.ndarray]:
    """Long when the close breaks above the highest high of the previous `period` bars; short
    when it breaks below the lowest low."""
    upper = rolling_max_prev(bars.high, p.period)
    lower = rolling_min_prev(bars.low, p.period)
    return bars.close > upper, bars.close < lower


class MaCrossParams(_Params):
    fast: int = Field(10, ge=2, le=500)
    slow: int = Field(30, ge=3, le=1000)
    type: Literal["sma", "ema"] = "sma"

    @model_validator(mode="after")
    def _fast_below_slow(self):
        if self.fast >= self.slow:
            raise ValueError("fast must be shorter than slow")
        return self


@LIBRARY.register("ma_cross", Kind.ENTRY, MaCrossParams)
def ma_cross(bars: Bars, p: MaCrossParams) -> tuple[np.ndarray, np.ndarray]:
    """Long when the fast moving average of closes crosses above the slow one; short when it
    crosses below."""
    fast = moving_average(bars.close, p.fast, p.type)
    slow = moving_average(bars.close, p.slow, p.type)
    return crossed_above(fast, slow), crossed_below(fast, slow)


class RsiThresholdParams(_Params):
    period: int = Field(14, ge=2, le=200)
    level: float = Field(30.0, gt=0, lt=100)
    mode: Literal["cross_up", "cross_down"] = "cross_up"


@LIBRARY.register("rsi_threshold", Kind.ENTRY, RsiThresholdParams)
def rsi_threshold(bars: Bars, p: RsiThresholdParams) -> tuple[np.ndarray, np.ndarray]:
    """RSI crossing a level, mirrored for shorts at 100 - level.

    cross_up: long when RSI crosses above `level`, short when it crosses below 100 - level.
    With level 30 that buys the recovery from oversold; with level 55 it buys momentum.
    cross_down: long when RSI crosses below `level`, short when it crosses above 100 - level.
    With level 30 that buys the dip as RSI falls into oversold.
    """
    value = rsi(bars.close, p.period)
    if p.mode == "cross_up":
        return crossed_above(value, p.level), crossed_below(value, 100 - p.level)
    return crossed_below(value, p.level), crossed_above(value, 100 - p.level)


class BollingerTouchParams(_Params):
    period: int = Field(20, ge=2, le=500)
    stdev: float = Field(2.0, gt=0, le=6)
    mode: Literal["fade", "break"] = "fade"


@LIBRARY.register("bollinger_touch", Kind.ENTRY, BollingerTouchParams)
def bollinger_touch(bars: Bars, p: BollingerTouchParams) -> tuple[np.ndarray, np.ndarray]:
    """The first close outside a Bollinger band (SMA plus or minus `stdev` standard deviations).

    fade: long on the first close below the lower band, short on the first close above the
    upper band. break: the opposite, long above the upper band and short below the lower.
    """
    middle = sma(bars.close, p.period)
    width = p.stdev * rolling_std(bars.close, p.period)
    above = crossed_above(bars.close, middle + width)
    below = crossed_below(bars.close, middle - width)
    return (below, above) if p.mode == "fade" else (above, below)


class PullbackToMaParams(_Params):
    trend_period: int = Field(100, ge=3, le=1000)
    pullback_period: int = Field(20, ge=2, le=500)
    depth_atr: float = Field(0.0, ge=0, le=5)

    @model_validator(mode="after")
    def _pullback_below_trend(self):
        if self.pullback_period >= self.trend_period:
            raise ValueError("pullback_period must be shorter than trend_period")
        return self


@LIBRARY.register("pullback_to_ma", Kind.ENTRY, PullbackToMaParams)
def pullback_to_ma(bars: Bars, p: PullbackToMaParams) -> tuple[np.ndarray, np.ndarray]:
    """Buy a pullback inside a trend; sell a rally inside a downtrend.

    Long at t when the close is above the trend SMA, the previous bar's low reached the
    pullback SMA (or `depth_atr` ATR(14) below it), and the close is back above the pullback
    SMA. Short is the mirror image.
    """
    trend = sma(bars.close, p.trend_period)
    fast = sma(bars.close, p.pullback_period)
    # Skip ATR when depth is 0, so its warm-up doesn't delay the first signals.
    if p.depth_atr:
        depth = p.depth_atr * atr(bars.high, bars.low, bars.close, 14)
    else:
        depth = np.zeros(len(bars))
    dip_line = fast - depth
    rally_line = fast + depth
    long = np.zeros(len(bars), dtype=np.bool_)
    short = np.zeros(len(bars), dtype=np.bool_)
    long[1:] = (
        (bars.close[1:] > trend[1:])
        & (bars.low[:-1] <= dip_line[:-1])
        & (bars.close[1:] > fast[1:])
    )
    short[1:] = (
        (bars.close[1:] < trend[1:])
        & (bars.high[:-1] >= rally_line[:-1])
        & (bars.close[1:] < fast[1:])
    )
    return long, short


class RangeBreakoutParams(_Params):
    session: Literal["asia", "london_open", "ny_open"] = "london_open"
    range_bars: int = Field(4, ge=1, le=48)


@LIBRARY.register("range_breakout", Kind.ENTRY, RangeBreakoutParams)
def range_breakout(bars: Bars, p: RangeBreakoutParams) -> tuple[np.ndarray, np.ndarray]:
    """Opening-range breakout.

    The range is the high and low of the bars in the first `range_bars` bar-lengths after the
    session opens (Tokyo 09:00, London 08:00 or New York 08:00 local, Monday to Friday). After
    the range, the first close above it is a long signal and the first close below it is a
    short signal, at most one of each per session day. A session day ends at the next open or
    24 hours after this one, whichever comes first.
    """
    long = np.zeros(len(bars), dtype=np.bool_)
    short = np.zeros(len(bars), dtype=np.bool_)
    if len(bars) == 0:
        return long, short
    opens = session_opens(p.session, int(bars.time[0]), int(bars.time[-1]))
    window = p.range_bars * bars.timeframe.seconds
    for k, opened in enumerate(opens):
        day_end = opened + 86400
        if k + 1 < len(opens):
            day_end = min(day_end, opens[k + 1])
        a, range_end, b = np.searchsorted(bars.time, [opened, opened + window, day_end])
        if range_end in (a, b):
            continue  # no bars in the range, or nothing after it
        top = bars.high[a:range_end].max()
        bottom = bars.low[a:range_end].min()
        after = bars.close[range_end:b]
        above = np.flatnonzero(after > top)
        below = np.flatnonzero(after < bottom)
        if len(above):
            long[range_end + above[0]] = True
        if len(below):
            short[range_end + below[0]] = True
    return long, short
