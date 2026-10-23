"""The Cost Model's inputs for one Trial (ADR 0004), and how swap nights are counted."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace

import numpy as np

from vibedesk.market.server_time import server_days


@dataclass(frozen=True, slots=True)
class CostSnapshot:
    """The cost assumptions one Trial ran with; every result keeps its snapshot."""

    point: float
    spread_floor_points: float  # median spread of recent real ticks
    slippage_points: float  # charged on every fill
    swap_long_points: float = 0.0  # per night held; negative means the position pays
    swap_short_points: float = 0.0
    triple_swap_day: int = 3  # MT5 numbering, 0 = Sunday: the rollover ending this day counts 3x
    spread_multiplier: float = 1.0  # 2.0 for the Gauntlet's spread stress check
    as_of: str = ""  # ISO date the floor and swaps were measured

    def spread_prices(self, bar_spread_points: np.ndarray) -> np.ndarray:
        """Spread charged per bar: max(recorded bar spread, Spread Floor), in price units."""
        floored = np.maximum(bar_spread_points, self.spread_floor_points)
        return floored * self.spread_multiplier * self.point

    @property
    def slippage_price(self) -> float:
        return self.slippage_points * self.point

    def stressed(self, multiplier: float = 2.0) -> CostSnapshot:
        return replace(self, spread_multiplier=multiplier)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> CostSnapshot:
        return cls(**json.loads(text))


def swap_nights(entry_utc: np.ndarray, exit_utc: np.ndarray, triple_day: int) -> np.ndarray:
    """Rollovers a position sits through, weighted the way MT5 charges swap.

    A rollover happens at each server midnight. A position is charged for one when it was open
    then: opened before it, and still open at the start of its exit bar. The rollovers that end
    Monday to Friday count once, the one ending `triple_day` counts three times (it covers the
    weekend), and the ones ending Saturday and Sunday count zero. A whole week is seven nights.
    """
    # weights[w] is the charge for the rollover that starts weekday w (Monday = 0), which ends
    # the day before it.
    ended = [(w - 1) % 7 for w in range(7)]
    triple = (triple_day - 1) % 7  # MT5 numbers Sunday 0; here Monday is 0
    weights = np.array([3 if e == triple else 1 if e < 5 else 0 for e in ended], dtype=np.int64)
    cumulative = np.cumsum(weights)

    def rollovers_up_to(utc: np.ndarray) -> np.ndarray:
        # 1970-01-01 (server day 0) was a Thursday, so weekday = (day + 3) % 7.
        shifted = server_days(utc) + 3
        return (shifted // 7) * weights.sum() + cumulative[shifted % 7]

    entry_utc = np.asarray(entry_utc, dtype=np.int64)
    exit_utc = np.asarray(exit_utc, dtype=np.int64)
    return rollovers_up_to(exit_utc) - rollovers_up_to(entry_utc)
