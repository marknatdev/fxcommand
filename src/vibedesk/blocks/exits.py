"""Exit Blocks. A stop returns its price distance per bar; a target receives the stop distance."""

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from vibedesk.blocks.indicators import atr
from vibedesk.blocks.registry import LIBRARY, Kind
from vibedesk.market.bars import Bars


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AtrStopParams(_Params):
    period: int = Field(14, ge=2, le=200)
    mult: float = Field(2.0, gt=0, le=20)


@LIBRARY.register("atr_stop", Kind.STOP, AtrStopParams)
def atr_stop(bars: Bars, p: AtrStopParams) -> np.ndarray:
    """Stop `mult` times ATR(`period`) away from the entry fill, using ATR at the signal bar."""
    return p.mult * atr(bars.high, bars.low, bars.close, p.period)


class RTargetParams(_Params):
    r: float = Field(2.0, gt=0, le=20)


@LIBRARY.register("r_target", Kind.TARGET, RTargetParams)
def r_target(bars: Bars, p: RTargetParams, stop_dist: np.ndarray) -> np.ndarray:
    """Target `r` times the stop distance away from the entry fill."""
    return p.r * stop_dist


class AtrTargetParams(_Params):
    period: int = Field(14, ge=2, le=200)
    mult: float = Field(3.0, gt=0, le=50)


@LIBRARY.register("atr_target", Kind.TARGET, AtrTargetParams)
def atr_target(bars: Bars, p: AtrTargetParams, stop_dist: np.ndarray) -> np.ndarray:
    """Target `mult` times ATR(`period`) away from the entry fill, using ATR at the signal bar."""
    return p.mult * atr(bars.high, bars.low, bars.close, p.period)


class AtrTrailingParams(_Params):
    period: int = Field(14, ge=2, le=200)
    mult: float = Field(3.0, gt=0, le=20)


@LIBRARY.register("atr_trailing", Kind.TRAILING, AtrTrailingParams)
def atr_trailing(bars: Bars, p: AtrTrailingParams) -> np.ndarray:
    """A trailing stop `mult` times ATR(`period`) behind each bar's close (the ask close for
    shorts). It is moved only at bar closes, only in the trade's favour, and takes over from
    the initial stop once it is tighter."""
    return p.mult * atr(bars.high, bars.low, bars.close, p.period)


class TimeExitParams(_Params):
    bars: int = Field(20, ge=1, le=1000)


@LIBRARY.register("time_exit", Kind.TIME, TimeExitParams)
def time_exit(bars: Bars, p: TimeExitParams) -> int:
    """Leave at the open of the bar `bars` bars after the entry bar, whatever the price."""
    return p.bars


class OppositeSignalParams(_Params):
    pass


@LIBRARY.register("opposite_signal", Kind.OPPOSITE, OppositeSignalParams)
def opposite_signal(bars: Bars, p: OppositeSignalParams) -> None:
    """Leave at the next open when the entry Block signals the other direction. Filters and
    the Spec's direction don't block this exit. When the Spec's own entry (filters and direction
    included) also signals that side at the same bar, the position reverses at that open."""
    return None
