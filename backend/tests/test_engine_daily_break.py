"""The engine through GOLD's daily break (23:57-01:00): a position that wants its stop moved while the
market is shut is retried quietly and managed again after the reopen; the Kill Switch in the break
raises one alert per position, not one per pass."""

import pytest

from fxcommand.broker.sim import DAY, SimBroker
from fxcommand.engine import AssignmentIn, SessionIn

from .conftest import FAST, MON_08, Harness

MON_2330 = MON_08 + 15 * 3600 + 30 * 60


def clock(h: Harness) -> int:
    return (h.sim.now % DAY) // 60


async def until(h: Harness, hh: int, mm: int) -> None:
    while clock(h) != hh * 60 + mm:
        await h.bars(1)


async def gold_position(tmp_path):
    h = Harness(tmp_path, sim=SimBroker(seed=11, start=MON_2330, history_days=5))
    await h.mgr.tick_once()
    gold = next(p for p in h.store.risk_profiles() if p.name == "Gold")
    s = await h.mgr.create_session(
        SessionIn(name="G", daily_loss_pct=90,
                  assignments=[AssignmentIn(symbol="GOLD", timeframe="M1", strategy="ema_cross", params=FAST, risk_profile_id=gold.id)])
    )
    await h.mgr.start(s.id)
    while not h.sim.positions(s.magic):
        assert clock(h) < 23 * 60 + 54, "no GOLD position before the rollover"
        await h.bars(1)
    return h, s


def entries(h, sid, since, kinds):
    return [j for j in h.store.journal(session_id=None, limit=10_000) if j.ts >= since and j.kind in kinds]


async def test_a_stop_move_refused_in_the_break_is_journaled_once_and_done_after_the_reopen(tmp_path):
    h, s = await gold_position(tmp_path)
    await until(h, 23, 56)
    [p] = h.sim.positions(s.magic)
    r = abs(p.price_open - p.sl)
    move = 1.5 * r / p.price_current * (1 if p.side == "long" else -1)
    await h.shock("GOLD", move)  # the last bar before the break: breakeven/trailing now wants to act
    assert clock(h) == 23 * 60 + 57 and not h.sim.market_open("GOLD")
    shut = h.sim.now
    await until(h, 1, 0)  # 63 engine passes with the market shut
    noisy = entries(h, s.id, shut, {"order_fail", "alert"})
    assert 1 <= len(noisy) <= 3, [j.message for j in noisy]
    assert all("market closed" in j.message for j in noisy if j.kind == "order_fail")
    reopen = h.sim.now
    await h.bars(10)
    if h.sim.positions(s.magic):  # still open: it was managed after the reopen
        assert entries(h, s.id, reopen, {"modify"}), "the stop was not moved after the reopen"
    h.close()


async def test_kill_switch_in_the_break_raises_one_alert_per_position(tmp_path):
    h, s = await gold_position(tmp_path)
    await until(h, 0, 30)
    t0 = h.sim.now
    await h.mgr.kill_all()
    await h.bars(10)
    fails = [j for j in entries(h, s.id, t0, {"order_fail"}) if j.message.startswith("Close #")]
    assert len(fails) == len(h.sim.positions(s.magic)) == 1  # left protected by its server-side stop
    await until(h, 1, 5)
    h.close()
