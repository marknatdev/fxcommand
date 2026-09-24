"""Going live (ADR 0006/0007): Order Outcomes, Orphans, close-until-confirmed, Kill Switch under a
hung Broker, Pinned Login, hedging-only, Live Caps, Equity Floor, Weekend Close, margin, symbol trade
mode, minimum-lot allowance and the Pre-flight Check — all on SimBroker with injected faults."""

import asyncio

import pytest

from fxcommand.broker import BrokerThread, BrokerTimeout
from fxcommand.broker.sim import SimBroker
from fxcommand.engine import DomainError
from fxcommand.engine.session_manager import in_weekend_close
from fxcommand.risk import RiskLimits

from .conftest import MON_08, Harness

FRI_22 = MON_08 + 4 * 86400 + 14 * 3600  # Friday 22:00 server time


async def until(h, pred, limit=300):
    for _ in range(limit):
        await h.bars(1)
        if pred():
            return True
    return False


def attempts(h, sid):
    return [j for j in h.store.journal(session_id=sid, kinds=["order", "order_fail"], limit=10_000)]


async def started(h, **kw):
    s = await h.session(symbols=kw.pop("symbols", ("EURUSD",)), **kw)
    await h.mgr.start(s.id)
    return s


async def live_harness(tmp_path, **sim_kw):
    sim = SimBroker(seed=11, start=MON_08, history_days=5, **sim_kw)
    sim.is_demo = False
    h = Harness(tmp_path, sim=sim)
    await h.mgr.tick_once()
    h.store.set_live_enabled(sim.login, True)
    return h


# ------------------------------------------------------------------ Order Outcomes
async def test_requote_is_retried_once_and_fills(h):
    s = await started(h)
    h.sim.inject("requote")
    assert await until(h, lambda: attempts(h, s.id))
    first = attempts(h, s.id)[-1]
    assert first.kind == "order" and "retried once after: requote" in first.message
    assert len(h.store.trades(session_id=s.id)) == 1


async def test_two_requotes_give_up_without_a_position(h):
    s = await started(h)
    h.sim.inject("requote", 2)
    assert await until(h, lambda: attempts(h, s.id))
    fail = attempts(h, s.id)[-1]
    assert fail.kind == "order_fail" and fail.alert and "retried once" in fail.message
    assert not h.sim.positions(s.magic) and not h.store.trades(session_id=s.id)


async def test_uncertain_filled_is_never_retried_and_is_adopted(h):
    s = await started(h)
    h.sim.inject("timeout_filled")
    assert await until(h, lambda: attempts(h, s.id))
    fail = attempts(h, s.id)[-1]
    assert fail.kind == "order_fail" and "UNCERTAIN" in fail.message and "found and adopted" in fail.message
    pos = h.sim.positions(s.magic)
    assert len(pos) == 1  # exactly one position: no second send
    row = h.store.trade_by_ticket(pos[0].ticket)
    assert row is not None and row.adopted and row.session_id == s.id


async def test_uncertain_not_filled_leaves_nothing_and_no_retry(h):
    s = await started(h)
    h.sim.inject("timeout_none")
    assert await until(h, lambda: attempts(h, s.id))
    assert "no position found" in attempts(h, s.id)[-1].message
    assert not h.sim.positions(s.magic) and not h.store.trades(session_id=s.id)


async def test_partial_fill_records_the_real_volume(h):
    s = await started(h)
    h.sim.inject("partial")
    assert await until(h, lambda: h.store.trades(session_id=s.id))
    t = h.store.trades(session_id=s.id)[0]
    p = next(p for p in h.sim.positions(s.magic) if p.ticket == t.ticket)
    assert t.volume == p.volume
    assert "partial fill" in attempts(h, s.id)[-1].message


async def test_zero_price_reply_uses_position_open_price(h):
    s = await started(h)
    h.sim.inject("price_zero")
    assert await until(h, lambda: h.store.trades(session_id=s.id))
    t = h.store.trades(session_id=s.id)[0]
    assert t.open_price > 0 and t.open_price == h.sim._positions[t.ticket].price_open
    assert t.initial_risk == pytest.approx(abs(t.open_price - t.sl))


async def test_orphan_is_adopted_on_the_next_pass(h):
    s = await started(h)
    t = h.sim.tick("EURUSD")
    orphan = h.sim.market_order("EURUSD", "long", 0.01, round(t.bid - 0.002, 5), 0.0, magic=s.magic)
    await h.mgr.tick_once()
    row = h.store.trade_by_ticket(orphan.ticket)
    assert row is not None and row.adopted
    alert = h.store.journal(alerts_only=True, limit=5)[0]
    assert "Orphan adopted" in alert.message


# ------------------------------------------------------------------ closes and Kill Switch
async def _with_position(h, **kw):
    s = await started(h, symbols=("EURUSD", "GBPUSD", "USDJPY"), **kw)
    assert await until(h, lambda: h.sim.positions(s.magic)), "no position opened"
    return s


async def test_close_is_retried_until_confirmed(h):
    s = await _with_position(h)
    h.sim.inject("close_fail", 3)
    await h.mgr.stop(s.id, close_positions=True)
    assert not h.sim.positions(s.magic)


async def test_close_that_answers_uncertain_is_confirmed_by_the_account(h):
    s = await _with_position(h)
    h.sim.inject("close_timeout_done")
    await h.mgr.stop(s.id, close_positions=True)
    assert not h.sim.positions(s.magic)
    assert not [j for j in h.store.journal(kinds=["order_fail"], limit=100) if "Close #" in j.message]


async def test_kill_switch_reports_positions_it_could_not_close(h):
    s = await _with_position(h)
    h.sim.inject("close_fail", 50)
    res = await h.mgr.kill_all()
    assert res["left_open"] >= 1
    assert "could NOT be closed" in h.store.journal(alerts_only=True, limit=1)[0].message
    h.sim.faults.clear()


async def test_failed_close_never_reverses(h):
    s = await _with_position(h)
    h.sim.inject("close_fail", 500)
    await h.bars(80)
    sides = {}  # the opposite side is never opened next to a position that could not be closed
    for p in h.sim.positions(s.magic):
        sides.setdefault(p.symbol, set()).add(p.side)
    assert all(len(v) == 1 for v in sides.values())
    h.sim.faults.clear()


async def test_broker_call_timeout():
    t = BrokerThread(SimBroker(seed=1, start=MON_08, history_days=2), timeout=0.2)
    with pytest.raises(BrokerTimeout):
        await t.run(lambda b: __import__("time").sleep(1.0))
    assert await t.run(lambda b: 1 + 1, timeout=5) == 2  # later calls queue behind the hung one, then work
    t._executor.shutdown(wait=True)


async def test_kill_switch_stops_sessions_while_the_broker_hangs(h):
    s = await _with_position(h)
    h.thread.timeout = 0.5
    h.sim.hang_seconds = 1.5
    h.sim.inject("hang", 3)
    busy = asyncio.create_task(h.bars(40))  # an entry will hang inside the broker thread
    for _ in range(200):
        await asyncio.sleep(0.02)
        if h.thread.busy_since:
            break
    kill = asyncio.create_task(h.mgr.kill_all())
    await asyncio.sleep(0.05)
    assert h.store.get_session(s.id).status == "stopped"  # immediately, before the engine lock is free
    await kill
    busy.cancel()
    try:
        await busy
    except (asyncio.CancelledError, Exception):
        pass
    await asyncio.sleep(3.5)
    h.sim.faults.clear()
    await h.mgr.tick_once()
    await h.mgr.kill_all()
    assert not h.sim.positions(s.magic)


# ------------------------------------------------------------------ account safety
async def test_netting_account_refused(h):
    h.sim.margin_mode = "netting"
    await h.mgr.tick_once()
    s = await h.session()
    with pytest.raises(DomainError) as e:
        await h.mgr.start(s.id)
    assert e.value.code == "netting_account"


async def test_account_switch_interrupts_sessions(h):
    s = await started(h)
    assert h.store.get_session(s.id).login == h.sim.login
    h.sim.login = 12345678
    await h.mgr.tick_once()
    row = h.store.get_session(s.id)
    assert row.status == "interrupted" and "account switched" in row.stop_reason
    assert "switched from account" in h.store.journal(alerts_only=True, limit=1)[0].message


async def test_live_caps_limit_volume_and_risk(tmp_path):
    h = await live_harness(tmp_path)
    h.store.set_live_caps({"max_risk_pct": 0.5, "max_volume": 0.02})
    s = await started(h, symbols=("EURUSD", "GBPUSD", "USDJPY"))
    assert await until(h, lambda: h.store.trades(session_id=s.id))
    t = h.store.trades(session_id=s.id)[0]
    assert t.volume <= 0.02 + 1e-9
    assert t.risk_amount <= h.sim.account().equity * 0.005 + 0.01
    msg = attempts(h, s.id)[-1].message
    assert "Live Caps" in msg
    h.close()


async def test_live_caps_do_not_apply_on_demo(h):
    h.store.set_live_caps({"max_risk_pct": 0.1, "max_volume": 0.01})
    s = await started(h)
    assert await until(h, lambda: h.store.trades(session_id=s.id))
    assert h.store.trades(session_id=s.id)[0].volume > 0.01


async def test_equity_floor_kills_and_blocks_until_reset(tmp_path):
    h = await live_harness(tmp_path)
    s = await started(h, symbols=("EURUSD", "GBPUSD", "USDJPY"))
    floor = h.store.equity_floor(h.sim.login)
    assert floor and floor["floor"] == pytest.approx(h.sim.account().equity * 0.8, abs=0.01)
    assert await until(h, lambda: h.sim.positions(s.magic))
    h.store.set_equity_floor(h.sim.login, {**floor, "floor": h.sim.account().equity + 1_000})
    await h.mgr.tick_once()
    assert h.store.get_session(s.id).status == "stopped"
    assert not h.sim.positions(s.magic)
    assert h.store.equity_floor(h.sim.login)["breached_at"]
    with pytest.raises(DomainError) as e:
        await h.mgr.start(s.id)
    assert e.value.code == "equity_floor"
    with pytest.raises(DomainError):
        await h.mgr.reset_equity_floor(confirm_login=1, pct=80)
    await h.mgr.reset_equity_floor(confirm_login=h.sim.login, pct=50)
    assert not h.store.equity_floor(h.sim.login)["breached_at"]
    await h.mgr.tick_once()
    await h.mgr.start(s.id)
    h.close()


async def test_live_start_requires_preflight(tmp_path):
    h = await live_harness(tmp_path)
    h.sim.algo_trading = False
    await h.mgr.tick_once()
    s = await h.session()
    with pytest.raises(DomainError) as e:
        await h.mgr.start(s.id)
    assert e.value.code == "preflight_failed" and "Algo Trading" in e.value.message
    h.sim.algo_trading = True
    await h.mgr.tick_once()
    await h.mgr.start(s.id)
    h.close()


async def test_preflight_report_on_demo_is_advisory(h):
    s = await h.session(symbols=("EURUSD", "GOLD"))
    rep = await h.mgr.preflight(s.id)
    assert rep["ok"] and not rep["live"] and not rep["enforced"]
    ids = {c["id"]: c for c in rep["checks"]}
    for k in ("terminal", "algo_trading", "hedging", "ping", "quote:EURUSD", "spread:GOLD", "margin:EURUSD", "min_lot:GOLD", "notifier", "database", "heartbeat"):
        assert k in ids, k
    assert ids["notifier"]["status"] == "warn" and not ids["notifier"]["blocking"]
    assert all(not c["blocking"] or c["id"] == "hedging" for c in rep["checks"])


async def test_symbol_trade_mode_is_respected(h):
    h.sim.trade_modes["EURUSD"] = "closeonly"
    s = await started(h)
    await h.bars(60)
    rej = h.store.journal(session_id=s.id, kinds=["risk_reject"], limit=100)
    assert rej and all("trade mode is closeonly" in j.message for j in rej)
    assert not h.store.trades(session_id=s.id)


async def test_margin_is_checked_before_sending(tmp_path):
    sim = SimBroker(seed=11, start=MON_08, history_days=5, leverage=1)
    h = Harness(tmp_path, sim=sim)
    await h.mgr.tick_once()
    s = await started(h)
    await h.bars(60)
    rej = h.store.journal(session_id=s.id, kinds=["risk_reject"], limit=100)
    assert rej and "margin" in rej[0].message
    assert not h.sim.positions()
    h.close()


async def test_min_lot_allowance(tmp_path):
    sim = SimBroker(seed=11, start=MON_08, history_days=5, balance=10.0)
    h = Harness(tmp_path, sim=sim)
    await h.mgr.tick_once()
    s = await started(h)
    await h.bars(40)
    assert any("minimum" in j.message for j in h.store.journal(session_id=s.id, kinds=["risk_reject"], limit=100))
    assert not h.store.trades(session_id=s.id)
    prof = h.store.risk_profiles()[0]
    prof.allow_min_lot, prof.min_lot_max_risk_pct = True, 50.0
    h.store.save_risk_profile(prof)
    assert await until(h, lambda: h.store.trades(session_id=s.id))
    t = h.store.trades(session_id=s.id)[0]
    assert t.volume == 0.01 and "minimum lot: real risk" in attempts(h, s.id)[-1].message
    h.close()


# ------------------------------------------------------------------ Weekend Close
def test_weekend_close_window():
    assert not in_weekend_close(FRI_22 - 3600, "22:30")
    assert not in_weekend_close(FRI_22, "22:30")
    assert in_weekend_close(FRI_22 + 30 * 60, "22:30")
    assert in_weekend_close(FRI_22 + 86400, "22:30")  # Saturday
    assert not in_weekend_close(MON_08, "22:30")


async def test_weekend_close_closes_and_blocks_entries(tmp_path):
    sim = SimBroker(seed=11, start=FRI_22 - 90 * 60, history_days=5)
    h = Harness(tmp_path, sim=sim)
    h.store.set_global_limits(RiskLimits(max_positions_global=10, daily_loss_pct_global=90))
    await h.mgr.tick_once()
    s = await started(h, symbols=("EURUSD", "GBPUSD", "USDJPY"), weekend_close=True, weekend_close_time="22:00", daily_loss_pct=90)
    assert await until(h, lambda: h.sim.positions(s.magic), limit=85)
    while h.sim.now < FRI_22 + 120:  # step past Friday 22:00
        await h.bars(1)
    assert not h.sim.positions(s.magic)
    assert any("Weekend Close" in j.message for j in h.store.journal(session_id=s.id, limit=500))
    orders_before = len(h.store.journal(session_id=s.id, kinds=["order"], limit=1000))
    await h.bars(30)  # still Friday evening: signals come, entries are skipped
    assert len(h.store.journal(session_id=s.id, kinds=["order"], limit=1000)) == orders_before
    assert any("entry skipped" in j.message for j in h.store.journal(session_id=s.id, kinds=["info"], limit=500))
    h.close()
