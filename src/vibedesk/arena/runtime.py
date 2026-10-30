"""Forward Arena runtime scheduler job and bar processor."""

from __future__ import annotations

import logging
from typing import Any

from vibedesk.arena.strategy import ArenaStepResult, ArenaStrategy, StrategyStatus

logger = logging.getLogger(__name__)


class ArenaRuntime:
    """Manages strategy execution on closed bars, gap detection and replay."""

    def __init__(
        self,
        root: Any,
        ledger: Any,
        api: Any | None = None,
    ) -> None:
        self.root = root
        self.ledger = ledger
        self.api = api
        self.strategies: dict[int, ArenaStrategy] = {}
        self._stopped: bool = False

    def register_strategy(self, strategy: ArenaStrategy) -> None:
        self.strategies[strategy.id] = strategy

    def process_closed_bar(
        self,
        symbol: str,
        timeframe: str,
        bar: dict[str, Any],
        live_spread: float | None = None,
    ) -> list[ArenaStepResult]:
        """Step all active strategies matching symbol and timeframe."""
        if self._stopped:
            return []

        results: list[ArenaStepResult] = []
        for s in self.strategies.values():
            if (
                s.symbol == symbol
                and s.timeframe == timeframe
                and s.status == StrategyStatus.ACTIVE
            ):
                try:
                    res = s.step(bar, live_spread=live_spread)
                    results.append(res)
                except NotImplementedError:
                    # Wave 0 stub fallback
                    pass
                except Exception as e:
                    logger.exception("Error stepping strategy #%s: %s", s.id, e)
        return results

    def check_and_recover_gaps(self) -> int:
        """Query cache for missed bars since last step and replay via catch_up.

        Teams implement in Wave 1 (CCC-66).
        """
        raise NotImplementedError("ArenaRuntime gap detection owned by Wave 1 (CCC-66)")
