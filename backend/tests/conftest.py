import asyncio
from dataclasses import replace

import pytest

from fxcommand.broker import BrokerThread
from fxcommand.broker.sim import SimBroker
from fxcommand.engine import AssignmentIn, SessionIn, SessionManager
from fxcommand.journal import EventBus, Journal
from fxcommand.store import Store

MON_08 = 1_704_700_800  # 2024-01-08 08:00 UTC (Monday)

# EMA 2/3 on M1 crosses constantly: guarantees signals in a few dozen bars
FAST = {"fast": 2, "slow": 3, "atr_period": 5, "sl_atr": 3.0, "tp_atr": 6.0}


class Harness:
    def __init__(self, tmp_path, sim: SimBroker | None = None, db_name="t.db"):
        self.sim = sim or SimBroker(seed=11, start=MON_08, history_days=5)
        self.thread = BrokerThread(self.sim)
        self.store = Store(f"sqlite:///{tmp_path / db_name}")
        self.bus = EventBus()
        self.bus.attach(asyncio.get_running_loop())
        self.journal = Journal(self.store, self.bus)
        self.mgr = SessionManager(self.thread, self.store, self.journal, self.bus)

    async def bars(self, n: int) -> None:
        for _ in range(n):
            await self.thread.run(lambda b: b.step(1))
            await self.mgr.tick_once()

    async def shock(self, symbol: str, pct: float) -> None:
        await self.thread.run(lambda b: b.shock(symbol, pct))
        await self.mgr.tick_once()

    def journal_kinds(self, session_id=None):
        return [j.kind for j in self.store.journal(session_id=session_id, limit=10_000)]

    async def session(self, name="S1", symbols=("EURUSD",), strategy="ema_cross", params=FAST, **kw):
        spec = SessionIn(
            name=name,
            assignments=[AssignmentIn(symbol=s, timeframe="M1", strategy=strategy, params=params) for s in symbols],
            **kw,
        )
        return await self.mgr.create_session(spec)

    def close(self):
        self.thread._executor.shutdown(wait=True)


@pytest.fixture
async def h(tmp_path):
    harness = Harness(tmp_path)
    await harness.mgr.tick_once()
    yield harness
    harness.close()


class LiveSim(SimBroker):
    """A SimBroker that reports itself as a LIVE account (for the live gate)."""

    def account(self):
        return replace(super().account(), is_demo=False)
