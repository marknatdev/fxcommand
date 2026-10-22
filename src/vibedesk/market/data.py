"""The market facade: the only way the Gauntlet, the Research Desk, the engine and the baseline
get bars (spec §4), so the Holdout guard (ADR 0002) cannot be walked around.
"""

from __future__ import annotations

from datetime import datetime

from vibedesk.market.bars import Bars, Timeframe
from vibedesk.market.grant import HOLDOUT_START, GrantVerifier, HoldoutGrant
from vibedesk.market.source import BarSource

_HOLDOUT_EPOCH = int(HOLDOUT_START.timestamp())


class HoldoutLocked(Exception):
    """Bars from the Holdout were asked for without a grant the Ledger approves."""


class MarketData:
    def __init__(self, source: BarSource, verifier: GrantVerifier) -> None:
        self._source = source
        self._verifier = verifier

    def bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        grant: HoldoutGrant | None = None,
    ) -> Bars:
        """Bars whose UTC open time is in [start, end).

        A range ending after HOLDOUT_START needs a grant the verifier approves. It is asked on
        every call and never cached, so a grant stops working as soon as the Ledger records
        its spend.
        """
        for name, value in (("start", start), ("end", end)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be a timezone-aware UTC datetime")
        if start >= end:
            raise ValueError("start must be before end")

        if end > HOLDOUT_START:
            if grant is None:
                raise HoldoutLocked(
                    f"{symbol} bars up to {end:%Y-%m-%d %H:%M} UTC reach the Holdout, which "
                    f"starts {HOLDOUT_START:%Y-%m-%d} UTC; the owner must approve a Holdout "
                    "Request first"
                )
            if not self._verifier.verify(grant, symbol):
                raise HoldoutLocked(
                    f"the Ledger does not approve Holdout grant {grant.request_id} for {symbol} "
                    "(forged, for another Idea Family or symbol, or already spent); the owner "
                    "must approve a Holdout Request first"
                )
            return self._source.fetch(symbol, timeframe, start, end)

        # A bar's OHLC covers prices up to its close, so an H4 or D1 bar that opens before
        # HOLDOUT_START but closes after it is Holdout data too.
        hi = min(int(end.timestamp()), _HOLDOUT_EPOCH - timeframe.seconds + 1)
        return self._source.fetch(symbol, timeframe, start, end).between(int(start.timestamp()), hi)
