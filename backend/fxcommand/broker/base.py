"""The Broker seam: the only way the system touches a market.

Two adapters satisfy it: ``Mt5Broker`` (the real terminal) and ``SimBroker``
(a seeded in-memory market). See docs/adr/0002-broker-seam-sim-and-mt5.md.

Interface contract (beyond the type signatures):

- Every method is synchronous and may block. Callers never call a Broker
  directly from the event loop: they go through ``BrokerThread``, which
  serialises every call onto one dedicated thread (the MT5 package is
  process-global and not thread-safe).
- ``closed_bars`` returns only *completed* bars, oldest first, as a DataFrame
  with columns time/open/high/low/close/volume (bid prices).
- ``market_order`` always carries a stop-loss; a Broker must reject an order
  whose stops are on the wrong side or closer than ``stops_level`` points.
- ``positions(magic)`` with ``magic=None`` returns every position on the
  Account, including foreign ones.
- ``history(since)`` returns positions closed at or after ``since``.
- ``market_order`` / ``close`` answer with an ``OrderResult`` whose ``outcome`` is filled,
  not_executed or uncertain (ADR 0007). A filled entry reports the *position's* ticket, open
  price and volume. Entries must never be retried after an uncertain outcome.
- ``margin_required`` is the Broker's own margin calculation for a market order now.
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Protocol, TypeVar

import pandas as pd

from .types import AccountInfo, ClosedTrade, OrderResult, Position, Side, SymbolInfo, TerminalStatus, Tick, Timeframe


class BrokerError(RuntimeError):
    """The Broker is unreachable or returned something unusable."""


class BrokerTimeout(BrokerError):
    """A Broker call did not return in time. The call may still complete on the broker thread."""


class Broker(Protocol):
    mode: str  # "sim" | "mt5"

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...
    def server_time(self) -> int: ...
    def terminal(self) -> TerminalStatus: ...
    def account(self) -> AccountInfo: ...
    def symbols(self) -> list[str]: ...
    def symbol_info(self, symbol: str) -> SymbolInfo: ...
    def tick(self, symbol: str) -> Tick: ...
    def closed_bars(self, symbol: str, timeframe: Timeframe, count: int) -> pd.DataFrame: ...
    def positions(self, magic: int | None = None) -> list[Position]: ...
    def history(self, since: int) -> list[ClosedTrade]: ...
    def market_order(
        self, symbol: str, side: Side, volume: float, sl: float, tp: float, magic: int, comment: str = ""
    ) -> OrderResult: ...
    def margin_required(self, symbol: str, side: Side, volume: float) -> float: ...
    def modify(self, ticket: int, sl: float, tp: float) -> OrderResult: ...
    def close(self, ticket: int, comment: str = "") -> OrderResult: ...


T = TypeVar("T")


class BrokerThread:
    """Runs every Broker call on a single dedicated thread, awaitable from asyncio."""

    def __init__(self, broker: Broker, timeout: float | None = 30.0):
        self.broker = broker
        self.timeout = timeout  # seconds an awaited call may take before BrokerTimeout
        self.busy_since: float | None = None  # wall time the running call started (None = idle)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="broker")

    @property
    def mode(self) -> str:
        return self.broker.mode

    def _call(self, fn: Callable[[Broker], T]) -> T:
        self.busy_since = time.time()
        try:
            return fn(self.broker)
        finally:
            self.busy_since = None

    async def run(self, fn: Callable[[Broker], T], timeout: float | None = None) -> T:
        """Run ``fn(broker)`` on the broker thread. Raises BrokerTimeout after ``timeout`` seconds
        (default ``self.timeout``); the thread cannot be interrupted, so the call may still finish
        later and later calls queue behind it."""
        fut = asyncio.wrap_future(self._executor.submit(self._call, fn))
        limit = self.timeout if timeout is None else timeout
        if not limit:
            return await fut
        try:
            return await asyncio.wait_for(asyncio.shield(fut), limit)
        except asyncio.TimeoutError:
            fut.add_done_callback(lambda f: f.cancelled() or f.exception())  # the late result is dropped
            raise BrokerTimeout(f"broker call did not return within {limit:g}s (terminal hung?)") from None

    def run_sync(self, fn: Callable[[Broker], T]) -> T:
        """For non-async callers (startup, tests): still serialised on the broker thread."""
        return self._executor.submit(self._call, fn).result()

    def shutdown(self) -> None:
        try:
            self.run_sync(lambda b: b.disconnect())
        finally:
            self._executor.shutdown(wait=True)
