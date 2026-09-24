import pytest

from fxcommand.engine import AssignmentIn, DomainError, SessionIn
from fxcommand.engine.stats import trade_stats

from .conftest import FAST, MON_08, Harness, LiveSim


async def test_session_trades_on_bar_close(h):
    s = await h.session(symbols=("EURUSD", "GBPUSD"))
    assert s.magic >= 770_001
    await h.mgr.start(s.id)
    await h.bars(60)
    kinds = h.journal_kinds(s.id)
    assert "signal" in kinds and "order" in kinds
    trades = h.store.trades(session_id=s.id)
    assert trades, "expected at least one trade in 60 bars"
    assert all(t.magic == s.magic and t.sl > 0 for t in trades)
    # at most one open position per assignment
    open_syms = [p.symbol for p in h.sim.positions(s.magic)]
    assert len(open_syms) == len(set(open_syms))
    # every order stamped with the session's magic
    assert all(p.magic == s.magic for p in h.sim.positions())


async def test_first_bar_after_start_is_not_traded(h):
    s = await h.session()
    await h.mgr.start(s.id)
    await h.mgr.tick_once()  # same bar as start: only watching
    assert "order" not in h.journal_kinds(s.id)
    state = next(iter(h.mgr.assignment_states(s.id).values()))
    assert state["status"].startswith("watching")


async def test_closes_are_reconciled_with_profit(h):
    s = await h.session(symbols=("EURUSD", "GBPUSD", "USDJPY"))
    await h.mgr.start(s.id)
    await h.bars(200)
    closed = h.store.trades(session_id=s.id, status="closed")
    assert closed, "expected closed trades after 200 bars"
    assert all(t.profit is not None and t.close_reason for t in closed)
    stats = trade_stats(h.store.trades(session_id=s.id))
    assert stats["trades"] == len(closed)
    # database matches broker history exactly
    hist = {c.ticket: c.profit for c in h.sim.history(0)}
    assert all(hist[t.ticket] == pytest.approx(t.profit) for t in closed)


async def test_pause_blocks_entries_but_keeps_positions(h):
    s = await h.session(symbols=("EURUSD", "GBPUSD", "USDJPY", "GOLD"))
    await h.mgr.start(s.id)
    await h.bars(40)
    await h.mgr.pause(s.id)
    n_orders = h.journal_kinds(s.id).count("order")
    await h.bars(60)
    assert h.journal_kinds(s.id).count("order") == n_orders
    assert h.store.get_session(s.id).status == "paused"
    with pytest.raises(DomainError):
        await h.mgr.pause(s.id)
    await h.mgr.resume(s.id)
    assert h.store.get_session(s.id).status == "running"


async def _session_with_position(h, **kw):
    s = await h.session(symbols=("EURUSD", "GBPUSD", "USDJPY"), **kw)
    await h.mgr.start(s.id)
    for _ in range(100):
        await h.bars(1)
        if h.sim.positions(s.magic):
            return s
    pytest.fail("no position opened")


async def test_stop_leave_positions(h):
    s = await _session_with_position(h)
    n = len(h.sim.positions(s.magic))
    await h.mgr.stop(s.id, close_positions=False)
    assert h.store.get_session(s.id).status == "stopped"
    assert len(h.sim.positions(s.magic)) == n
    n_orders = h.journal_kinds(s.id).count("order")
    await h.bars(30)
    assert h.journal_kinds(s.id).count("order") == n_orders  # stopped => no trading


async def test_stop_close_positions(h):
    s = await _session_with_position(h)
    await h.mgr.stop(s.id, close_positions=True)
    assert h.sim.positions(s.magic) == []
    assert h.store.open_trades(s.id) == []


async def test_symbol_rules(h):
    with pytest.raises(DomainError) as e:
        await h.mgr.create_session(
            SessionIn(name="dup", assignments=[AssignmentIn(symbol="EURUSD"), AssignmentIn(symbol="EURUSD", timeframe="H1")])
        )
    assert e.value.code == "duplicate_symbol"
    a = await h.session("A", symbols=("EURUSD", "GOLD"))
    b = await h.session("B", symbols=("GBPUSD", "GOLD"))
    await h.mgr.start(a.id)
    with pytest.raises(DomainError) as e:
        await h.mgr.start(b.id)
    assert e.value.code == "symbol_conflict" and "GOLD" in e.value.message
    await h.mgr.stop(a.id)
    await h.mgr.start(b.id)  # fine once A is stopped
    with pytest.raises(DomainError) as e:
        await h.session("A", symbols=("USDJPY",))
    assert e.value.code == "name_taken"


async def test_edit_and_delete_rules(h):
    s = await h.session()
    await h.mgr.start(s.id)
    spec = SessionIn(name="S1", assignments=[AssignmentIn(symbol="GBPUSD")])
    with pytest.raises(DomainError):
        await h.mgr.update_session(s.id, spec)
    with pytest.raises(DomainError):
        await h.mgr.delete_session(s.id)
    await h.mgr.stop(s.id)
    s2 = await h.mgr.update_session(s.id, spec)
    assert s2.magic == s.magic and [a.symbol for a in h.store.assignments(s.id)] == ["GBPUSD"]
    await h.mgr.delete_session(s.id)
    new = await h.session("S2")
    assert new.magic > s.magic  # magic numbers are never reused


async def test_daily_loss_auto_stop(h):
    s = await _session_with_position(h, daily_loss_pct=0.5)
    p = h.sim.positions(s.magic)[0]
    await h.shock(p.symbol, -0.03 if p.side == "long" else 0.03)  # gap through the stop-loss
    row = h.store.get_session(s.id)
    assert row.status == "stopped" and "AUTO-STOP" in row.stop_reason
    assert h.sim.positions(s.magic) == []  # close_on_auto_stop defaults to True
    alerts = h.store.journal(session_id=s.id, alerts_only=True)
    assert any("AUTO-STOP" in a.message for a in alerts)


async def test_kill_switch_closes_owned_only(h):
    a = await _session_with_position(h)
    t = h.sim.tick("GOLD")
    foreign = h.sim.market_order("GOLD", "long", 0.01, round(t.bid - 20, 2), 0, magic=12345)
    assert foreign.ok
    res = await h.mgr.kill_all()
    assert res["closed_positions"] >= 1
    assert h.store.get_session(a.id).status == "stopped"
    remaining = h.sim.positions()
    assert [p.ticket for p in remaining] == [foreign.ticket]  # foreign position untouched
    assert any("KILL SWITCH" in j.message for j in h.store.journal(alerts_only=True))
    with pytest.raises(DomainError) as e:
        await h.mgr.close_position(foreign.ticket)
    assert e.value.code == "foreign_position"


async def test_restart_interrupts_and_readopts(tmp_path, h):
    s = await _session_with_position(h, name="Resumer")
    tickets = {p.ticket for p in h.sim.positions(s.magic)}
    # a position the DB doesn't know about (e.g. crash between send and commit)
    t = h.sim.tick("GOLD")
    orphan = h.sim.market_order("GOLD", "short", 0.01, round(t.ask + 20, 2), 0, magic=s.magic)
    # "restart": new manager over the same database and broker
    h2 = Harness(tmp_path, sim=h.sim)
    await h2.mgr.boot()
    assert h2.store.get_session(s.id).status == "interrupted"
    await h2.mgr.start(s.id)
    adopted = h2.store.trade_by_ticket(orphan.ticket)
    assert adopted is not None and adopted.adopted and adopted.session_id == s.id
    assert tickets <= {t.ticket for t in h2.store.open_trades(s.id)}
    h2.close()


async def test_auto_resume_on_boot(tmp_path, h):
    s = await h.session(auto_resume=True)
    await h.mgr.start(s.id)
    h2 = Harness(tmp_path, sim=h.sim)
    await h2.mgr.boot()
    assert h2.store.get_session(s.id).status == "running"
    h2.close()


async def test_live_account_requires_live_enabled(tmp_path):
    h = Harness(tmp_path, sim=LiveSim(seed=2, start=MON_08, history_days=5))
    await h.mgr.tick_once()
    s = await h.session()
    with pytest.raises(DomainError) as e:
        await h.mgr.start(s.id)
    assert e.value.code == "live_blocked"
    h.store.set_live_enabled(h.sim.account().login, True)
    await h.mgr.start(s.id)
    h.close()


async def test_risk_rejection_is_journaled(h):
    # spread limit below EURUSD's 12 pts -> every entry rejected with a reason
    tight = h.store.risk_profiles()[0]
    tight.max_spread_points = 5
    h.store.save_risk_profile(tight)
    s = await h.session()
    await h.mgr.start(s.id)
    await h.bars(60)
    rejects = h.store.journal(session_id=s.id, kinds=["risk_reject"])
    assert rejects and "spread" in rejects[0].message
    assert h.sim.positions(s.magic) == []


async def test_breakeven_moves_stop(h):
    prof = h.store.risk_profiles()[0]
    prof.breakeven, prof.breakeven_at_r = True, 0.3
    h.store.save_risk_profile(prof)
    s = await _session_with_position(h)
    p = h.sim.positions(s.magic)[0]
    r = abs(p.price_open - p.sl)
    move = (0.6 * r + 20 * h.sim.symbol_info(p.symbol).point) / p.price_open  # past 0.3R net of spread, short of TP (2R)
    await h.shock(p.symbol, move if p.side == "long" else -move)
    assert "modify" in h.journal_kinds(s.id)


async def test_snapshot_shape(h):
    s = await _session_with_position(h)
    snap = h.mgr.snapshot()
    assert snap["mode"] == "sim" and snap["connected"]
    assert snap["account"]["login"] == 99_000_001
    assert any(p["owned"] and p["session_id"] == s.id for p in snap["positions"])
    assert snap["sessions"][0]["status"] == "running"
