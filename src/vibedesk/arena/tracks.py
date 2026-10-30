"""Two fill tracks: Backtest-Cost Track and Live-Spread Track."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FillTrackKind(StrEnum):
    BACKTEST_COST = "backtest_cost"
    LIVE_SPREAD = "live_spread"


@dataclass(frozen=True)
class FillPricing:
    """Pricing calculation for a simulated fill."""

    track: FillTrackKind
    fill_price: float
    spread_points: float
    slippage_points: float
    total_cost: float


def price_fill(
    track: FillTrackKind,
    bar_open: float,
    direction: int,
    point: float,
    spread_floor: float,
    bar_spread: float,
    live_spread: float | None = None,
    slippage_points: float = 1.0,
) -> FillPricing:
    """Compute fill price and applied friction for a given track.

    - Backtest-Cost Track: uses max(bar_spread, spread_floor) + 1 pt slippage.
    - Live-Spread Track: uses live_spread (or bar_spread if live unavailable) + 1 pt slippage.
    """
    if track == FillTrackKind.BACKTEST_COST:
        effective_spread = max(bar_spread, spread_floor)
    else:
        effective_spread = live_spread if live_spread is not None else bar_spread

    # For longs (direction=1), pay ask = open + spread
    # For shorts (direction=-1), sell bid = open
    spread_cost = effective_spread * point
    slippage_cost = slippage_points * point

    if direction == 1:
        exec_price = bar_open + spread_cost + slippage_cost
    else:
        exec_price = bar_open - slippage_cost

    total_cost = spread_cost + slippage_cost
    return FillPricing(
        track=track,
        fill_price=exec_price,
        spread_points=effective_spread,
        slippage_points=slippage_points,
        total_cost=total_cost,
    )
