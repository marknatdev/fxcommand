"""Cost Check: what one round trip costs an Assignment, in R, before it is allowed to start (spec D4, D14, D38).

Only spread and slippage block. They are paid on every trade whatever happens next, and they are what
made GOLD M1 trading lose. Swap is reported beside them but never blocks: whether an edge
survives its swap is a question for the Evidence, not for this check.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

DEFAULT_MAX_COST_R = 0.15


@dataclass(frozen=True)
class CostEstimate:
    cost_r: float  # spread + entry and exit slippage, as a share of the stop distance
    swap_r: float  # expected swap per trade as a share of the stop distance (signed: negative = a cost)
    threshold: float
    blocked: bool
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def estimate_cost(spread: float, slippage: float, stop_dist: float, threshold: float = DEFAULT_MAX_COST_R, swap: float = 0.0) -> CostEstimate:
    """``spread``, ``slippage`` and ``stop_dist`` in price units; ``swap`` is the expected signed swap per
    trade in price units (e.g. swap per night × typical nights held)."""
    if not stop_dist or stop_dist <= 0:
        return CostEstimate(float("inf"), 0.0, threshold, True, "no stop distance: cost cannot be judged")
    cost_r = (spread + 2 * slippage) / stop_dist
    swap_r = swap / stop_dist
    blocked = cost_r > threshold
    if blocked:
        reason = f"costs {cost_r:.2f}R per trade (spread + slippage), above the {threshold:.2f}R limit"
    else:
        reason = f"costs {cost_r:.2f}R per trade, within the {threshold:.2f}R limit"
    if swap_r:
        reason += f"; swap {swap_r:+.2f}R per trade (shown, not blocking)"
    return CostEstimate(round(cost_r, 4), round(swap_r, 4), threshold, blocked, reason)
