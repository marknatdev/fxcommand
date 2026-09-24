"""Paper Execution Mode (ADR 0007): the whole engine on real prices, fills in a local book, nothing
sent to the Account — and the book physically cannot send an order."""

import pytest

from fxcommand.broker.paper import PaperViolation, ReadOnlyBroker
from fxcommand.broker.sim import SimBroker
from fxcommand.engine import DomainError
from fxcommand.risk import RiskLimits

from .conftest import FAST, MON_08, Harness


async def paper_session(h, symbols=("EURUSD", "GBPUSD", "USDJPY"), **kw):
    s = await h.session(symbols=symbols, execution="paper", **kw)
    await h.mgr.start(s.id)
    return s


async def test_paper_session_trades_without_touching_the_account(h):
    s = await paper_session(h)
    await h.bars(150)
    trades = h.store.trades(session_id=s.id)
    assert trades and all(t.paper and t.ticket < 0 for t in trades)
    assert not h.sim.positions() and not h.sim.history(0)  # the Account never saw an order
    assert h.sim.account().balance == 10_000.0
    closed = [t for t in trades if t.status == "closed"]
    assert closed and all(t.profit is not None and t.close_reason for t in closed)
    orders = h.store.journal(session_id=s.id, kinds=["order"], limit=100)
    assert orders and all(j.message.startswith("PAPER Opened") for j in orders)


async def test_paper_pnl_is_excluded_from_account_limits(h):
    s = await paper_session(h, daily_loss_pct=90)
    await h.bars(150)
    day_ts, _ = h.mgr._day_start()
    assert h.store.realized_since(day_ts) == 0.0  # Account P&L: paper excluded
    assert h.store.realized_since(day_ts, s.id) != 0.0
    snap = h.mgr.snapshot()
    assert all(p["paper"] for p in snap["positions"] if p["magic"] == s.magic)


async def test_paper_stop_always_closes(h):
    s = await paper_session(h)
    for _ in range(200):
        await h.bars(1)
        if any(p.magic == s.magic for p in h.mgr._positions):
            break
    await h.mgr.stop(s.id, close_positions=False)
    assert not [p for p in h.mgr._positions if p.magic == s.magic]
    assert not h.store.paper_open()


async def test_paper_and_broker_sessions_side_by_side(h):
    h.store.set_global_limits(RiskLimits(max_positions_global=10, daily_loss_pct_global=90))
    paper = await paper_session(h, symbols=("EURUSD",), name="Paper")
    real = await h.session(name="Real", symbols=("GBPUSD",), daily_loss_pct=90)
    await h.mgr.start(real.id)
    await h.bars(120)
    assert all(t.paper for t in h.store.trades(session_id=paper.id))
    assert all(not t.paper and t.ticket > 0 for t in h.store.trades(session_id=real.id))
    assert all(p.magic == real.magic for p in h.sim.positions())
    await h.mgr.kill_all()
    assert not h.mgr._positions  # the Kill Switch closes Paper Positions too


async def test_symbol_rule_applies_across_modes(h):
    await paper_session(h, symbols=("EURUSD",), name="Paper")
    other = await h.session(name="Real", symbols=("EURUSD",))
    with pytest.raises(DomainError) as e:
        await h.mgr.start(other.id)
    assert e.value.code == "symbol_conflict"


async def test_paper_runs_on_a_live_account_without_live_enable(tmp_path):
    sim = SimBroker(seed=11, start=MON_08, history_days=5)
    sim.is_demo = False
    h = Harness(tmp_path, sim=sim)
    await h.mgr.tick_once()
    s = await paper_session(h)
    await h.bars(100)
    assert h.store.trades(session_id=s.id) and not h.sim.positions()
    h.close()


async def test_execution_switch_needs_flat_and_live_confirmation(tmp_path):
    sim = SimBroker(seed=11, start=MON_08, history_days=5)
    sim.is_demo = False
    h = Harness(tmp_path, sim=sim)
    await h.mgr.tick_once()
    s = await paper_session(h)
    await h.mgr.stop(s.id)
    from fxcommand.engine import AssignmentIn, SessionIn

    spec = dict(name=s.name, assignments=[AssignmentIn(symbol="EURUSD", timeframe="M1", params=FAST)], execution="broker")
    with pytest.raises(DomainError) as e:
        await h.mgr.update_session(s.id, SessionIn(**spec))
    assert e.value.code == "confirm_required"
    row = await h.mgr.update_session(s.id, SessionIn(**spec, confirm_login=sim.login))
    assert row.execution == "broker"
    h.close()


async def test_paper_positions_are_settled_from_missed_bars_after_restart(tmp_path, h):
    s = await paper_session(h)
    for _ in range(300):
        await h.bars(1)
        if h.store.paper_open():
            break
    row = h.store.paper_open()[0]
    # the app is "down": the market moves 600 bars without an engine pass
    h.sim.step(600)
    h2 = Harness(tmp_path, sim=h.sim)
    await h2.mgr.boot()
    assert h2.store.get_session(s.id).status == "interrupted"
    await h2.mgr.tick_once()
    settled = next(r for r in h2.store.paper_closed_since(0) if r.ticket == row.ticket)
    assert settled.reason in ("sl", "tp")
    assert settled.time_close < h.sim.now - 60  # at the bar that hit it, not at restart time
    t = h2.store.trade_by_ticket(row.ticket)
    assert t.status == "closed" and t.close_reason == settled.reason
    h2.close()


def test_paper_book_cannot_reach_order_methods():
    ro = ReadOnlyBroker(SimBroker(seed=1, start=MON_08, history_days=2))
    assert ro.tick("EURUSD").bid > 0  # reads pass through
    for call in (lambda: ro.market_order("EURUSD", "long", 0.01, 1.0, 0, 1), lambda: ro.modify(1, 1.0, 0), lambda: ro.close(1)):
        with pytest.raises(PaperViolation):
            call()


async def test_paper_fills_pay_one_point_of_slippage(h):
    s = await paper_session(h, symbols=("EURUSD",))
    for _ in range(200):
        await h.bars(1)
        if h.store.trades(session_id=s.id):
            break
    order = h.store.journal(session_id=s.id, kinds=["order"], limit=1)[0].data
    side = h.store.trade_by_ticket(order["ticket"]).side
    worse = order["price"] - order["entry"] if side == "long" else order["entry"] - order["price"]
    assert worse == pytest.approx(1e-5)
