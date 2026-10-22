"""A market prepared once for many simulations.

A Trial simulates the same bars thousands of times: every grid Combination, the spread stress
and the Coin-Flip Baseline. What the engine needs besides the signals is the same every time:
the spread charged on each bar and each M1 minute, which minutes fall inside each bar, and how
many swap rollovers lie between any two bars. `prepare` computes that once.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from vibedesk.engine.costs import CostSnapshot, swap_nights
from vibedesk.market.bars import Bars


@dataclass(frozen=True, slots=True, eq=False)
class PreparedMarket:
    """Bars plus everything the engine derives from them and one CostSnapshot.

    The arrays are read-only because one market is shared by every simulation of a Trial.
    """

    bars: Bars
    cost: CostSnapshot
    spread: np.ndarray  # spread charged on each bar, in price units
    slippage: float  # charged on every fill, in price units
    rollovers: np.ndarray  # weighted swap rollovers from the first bar to each bar
    never: np.ndarray  # all False: no exit signal
    no_trail: np.ndarray  # all NaN: no trailing stop
    m1_open: np.ndarray  # empty without M1
    m1_high: np.ndarray
    m1_low: np.ndarray
    m1_spread: np.ndarray  # spread charged on each minute, in price units
    m1_lo: np.ndarray  # minutes m1_lo[t]:m1_hi[t] lie inside bar t; all 0 without M1
    m1_hi: np.ndarray

    def __len__(self) -> int:
        return len(self.bars)

    @property
    def time(self) -> np.ndarray:
        return self.bars.time

    @property
    def has_m1(self) -> bool:
        return len(self.m1_open) > 0


def prepare(bars: Bars, cost: CostSnapshot, m1: Bars | None = None) -> PreparedMarket:
    """Prepare `bars` under `cost`; `cost.stressed()` prepares the spread-stress market.

    `m1`, when given, settles bars where the stop and the target are both touched.
    """
    n = len(bars)
    # Swap nights between two bars are rollovers[exit] - rollovers[entry]: swap_nights is a
    # difference of per-time counts, so counting from the first bar once gives the same integers.
    first = bars.time[:1] if n else bars.time
    rollovers = swap_nights(first, bars.time, cost.triple_swap_day)
    if m1 is None:
        m1_open = m1_high = m1_low = m1_spread = np.zeros(0)
        m1_lo = m1_hi = np.zeros(n, dtype=np.int64)
    else:
        m1_open, m1_high, m1_low = m1.open, m1.high, m1.low
        m1_spread = cost.spread_prices(m1.spread)
        m1_lo = np.searchsorted(m1.time, bars.time).astype(np.int64)
        m1_hi = np.searchsorted(m1.time, bars.time + bars.timeframe.seconds).astype(np.int64)
    arrays = {
        "spread": cost.spread_prices(bars.spread),
        "rollovers": rollovers,
        "never": np.zeros(n, dtype=np.bool_),
        "no_trail": np.full(n, np.nan),
        "m1_open": m1_open,
        "m1_high": m1_high,
        "m1_low": m1_low,
        "m1_spread": m1_spread,
        "m1_lo": m1_lo,
        "m1_hi": m1_hi,
    }
    for array in arrays.values():
        array.setflags(write=False)
    return PreparedMarket(bars=bars, cost=cost, slippage=cost.slippage_price, **arrays)
