"""The seam between Vibe Desk and wherever bars come from (MT5, or synthetic data in tests)."""

from datetime import datetime
from typing import Protocol

from vibedesk.market.bars import Bars, Timeframe


class BarSource(Protocol):
    def fetch(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Bars:
        """Bars whose UTC open time is in [start, end); a missing bound means unbounded."""
        ...
