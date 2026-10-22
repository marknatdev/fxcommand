"""Entry Blocks: each returns (long, short) boolean arrays of signals read at bar close."""

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from vibedesk.blocks.indicators import rolling_max_prev, rolling_min_prev
from vibedesk.blocks.registry import LIBRARY, Kind
from vibedesk.market.bars import Bars


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
