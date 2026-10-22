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
