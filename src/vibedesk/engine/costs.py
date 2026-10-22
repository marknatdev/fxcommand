"""The Cost Model's inputs for one Trial (ADR 0004)."""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class CostSnapshot:
    """The cost assumptions one Trial ran with; every result keeps its snapshot."""

    point: float
    spread_floor_points: float  # median spread of recent real ticks
    slippage_points: float  # charged on every fill

    def spread_prices(self, bar_spread_points: np.ndarray) -> np.ndarray:
        """Spread charged per bar: max(recorded bar spread, Spread Floor), in price units."""
        return np.maximum(bar_spread_points, self.spread_floor_points) * self.point

    @property
    def slippage_price(self) -> float:
        return self.slippage_points * self.point
