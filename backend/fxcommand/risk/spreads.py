"""The typical spread of a Symbol: what the Cost Check and Learning price a round trip at (spec D26).

Only good quotes count: fresh (the market is open) and outside the rollover minutes, when a market is
shut or quotes several times its usual spread (GOLD's 00:00 break, where GOLD Reopen Drift's signal
bar closes). The median of the recent good quotes is kept per Symbol and persisted, so a Session
started right after a restart is judged on a real spread, never on one quote from the break.
"""

from __future__ import annotations

import collections
import statistics

from ..broker.types import Tick

ROLLOVER = (23 * 60 + 55, 10)  # server minutes [23:55, 00:10)
FRESH_SECONDS = 120  # an older quote means the market is shut (as the Risk Gate judges it)
SAMPLES = 60
SETTING = "typical_spreads"


def good_quote(tick: Tick, now: int) -> bool:
    minute = (now % 86400) // 60
    return now - tick.time <= FRESH_SECONDS and not (minute >= ROLLOVER[0] or minute < ROLLOVER[1])


class SpreadBook:
    def __init__(self, store):
        self.store = store
        self._recent: dict[str, collections.deque] = {}
        self._count: dict[str, int] = {}

    def sample(self, symbol: str, tick: Tick, now: int) -> None:
        if not good_quote(tick, now):
            return
        q = self._recent.setdefault(symbol, collections.deque(maxlen=SAMPLES))
        q.append(max(tick.ask - tick.bid, 0.0))
        self._count[symbol] = n = self._count.get(symbol, 0) + 1
        if n % 10 == 1:  # persist now and then: it must survive a restart, not every quote
            saved = self.store.get_setting(SETTING, {}) or {}
            saved[symbol] = statistics.median(q)
            self.store.set_setting(SETTING, saved)

    def typical(self, symbol: str) -> float | None:
        """The median of recent good quotes, else the last persisted one, else None."""
        q = self._recent.get(symbol)
        if q:
            return float(statistics.median(q))
        saved = (self.store.get_setting(SETTING, {}) or {}).get(symbol)
        return float(saved) if saved is not None else None

    def spread_for(self, symbol: str, tick: Tick, now: int) -> float | None:
        """Sample ``tick`` and return the typical spread; the current quote only while nothing else is
        known and the quote is good. None: nothing trustworthy yet (the market is shut)."""
        self.sample(symbol, tick, now)
        return self.typical(symbol)
