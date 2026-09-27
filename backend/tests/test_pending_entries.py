"""Pending Entries and deferred exits (spec D5/D12/D13/D20): a Signal that lands while GOLD is shut
waits and is sent at the reopen through ``_enter``; its fill window ends it; exits wait for the
market instead of raising "close it in the terminal"; Stop, Kill Switch and Champion changes cancel."""

import pytest

from fxcommand.broker.sim import DAY, SimBroker
from fxcommand.engine import AssignmentIn, SessionIn, SessionManager

from .conftest import MON_08, Harness
from .test_learning_service import LH

MON_18 = MON_08 + 10 * 3600


def clock(h) -> int:
    return (h.sim.now % DAY) // 60


async def until(h, hh, mm):
    while clock(h) != hh * 60 + mm:
        await h.bars(1)


def profile_id(h, name):
    return next(p.id for p in h.store.risk_profiles() if p.name == name)


async def reopen_session(h, profile="Gold Reopen", execution="broker", name="Reopen"):
    s = await h.mgr.create_session(
        SessionIn(name=name, execution=execution, daily_loss_pct=90,
                  assignments=[AssignmentIn(symbol="GOLD", timeframe="H1", strategy="session_drift", risk_profile_id=profile_id(h, profile))])
    )
    await h.mgr.start(s.id)
    return s


def kinds(h, s, kind):
    return [j for j in h.store.journal(session_id=s.id, kinds=[kind], limit=10_000)]


@pytest.mark.parametrize("execution", ["broker", "paper"])
async def test_reopen_entries_wait_for_01_00_and_match_the_shadow_trades(tmp_path, execution):
    h = LH(tmp_path, sim=SimBroker(seed=4, start=MON_18, history_days=10))
    await h.mgr.tick_once()
    s = await reopen_session(h, execution=execution)
    await h.bars(int(1.6 * DAY / 60))  # Tuesday's and Wednesday's reopens
    trades = h.store.trades(session_id=s.id)
    shadows = [r for r in h.L.repo.shadow_closed("GOLD", "H1") if r.open_ts < h.sim.now - 3600]
    assert len(trades) >= 2 and all(t.paper == (execution == "paper") for t in trades)
    for t in trades:
        assert (t.open_time % DAY) // 60 in range(60, 65), t.open_time  # at the reopen, within the 5-minute fill window
    assert sorted(t.open_time // 3600 for t in trades) == sorted(r.open_ts // 3600 for r in shadows)  # live = shadow
    deferred = [j for j in kinds(h, s, "risk_reject") if "deferred" in j.message]
    assert len(deferred) == len(trades)  # journaled once per signal, not once per pass
    assert not [j for j in h.store.journal(session_id=s.id, kinds=["order_fail"], limit=100)]
    assert [r.status for r in h.store.pending_entries(s.id, status=None)] == ["filled"] * len(trades)
    # fill quality on the Learning Signal record (D32): the Signal closes with the 23:00 bar at 00:00, so the wait
    # is the daily break plus the minute to the first tradable bar; the spread is the reopen's
    taken = [r for r in h.L.repo.signals("GOLD", "H1", limit=50) if r.ticket is not None]
    assert len(taken) == len(trades)
    for r in taken:
        assert 3600 <= r.fill_delay_s < 3600 + 5 * 60 and r.fill_spread_points >= 60, (r.fill_delay_s, r.fill_spread_points)
    opened = [j for j in kinds(h, s, "order") if "Opened" in j.message]
    assert all(j.data["fill_delay_s"] is not None and j.data["fill_spread_points"] for j in opened)
    await h.close()


async def test_on_the_gold_profile_the_reopen_spread_outlasts_the_fill_window(tmp_path):
    h = LH(tmp_path, sim=SimBroker(seed=4, start=MON_18, history_days=10))
    await h.mgr.tick_once()
    s = await reopen_session(h, profile="Gold")  # 60-point cap; the reopen quotes 70 until 01:15
    await h.bars(int(0.6 * DAY / 60))
    assert h.store.trades(session_id=s.id) == []
    [r] = h.store.pending_entries(s.id, status=None)
    assert r.status == "expired" and r.expires_ts - r.first_tradable_ts == 300
    assert len([j for j in kinds(h, s, "risk_reject") if "deferred" in j.message]) == 1
    assert h.L.repo.signals("GOLD", "H1")[0].decision == "rejected"  # the Learning record gets the final outcome only
    await h.close()


async def test_a_paused_session_never_sends_and_a_stop_cancels(tmp_path):
    h = LH(tmp_path, sim=SimBroker(seed=4, start=MON_18, history_days=10))
    await h.mgr.tick_once()
    s = await reopen_session(h)
    await until(h, 0, 30)
    [r] = h.store.pending_entries(s.id)
    await h.mgr.pause(s.id)
    await until(h, 1, 10)
    assert h.store.trades(session_id=s.id) == [] and h.store.pending_entries(s.id, status=None)[0].status == "expired"
    await h.mgr.resume(s.id)
    await until(h, 0, 30)  # the next night
    assert h.store.pending_entries(s.id)
    await h.mgr.stop(s.id)
    assert not h.store.pending_entries(s.id)
    assert h.store.pending_entries(s.id, status=None)[-1].status == "cancelled"
    await h.close()


async def test_the_kill_switch_cancels_pending_entries(tmp_path):
    h = LH(tmp_path, sim=SimBroker(seed=4, start=MON_18, history_days=10))
    await h.mgr.tick_once()
    s = await reopen_session(h)
    await until(h, 0, 30)
    assert h.store.pending_entries(s.id)
    await h.mgr.kill_all()
    await until(h, 1, 10)
    assert not h.store.pending_entries(s.id) and h.store.trades(session_id=s.id) == []
    await h.close()


async def test_a_pending_entry_survives_a_restart_and_is_sent_if_restarted_in_time(tmp_path):
    sim = SimBroker(seed=4, start=MON_18, history_days=10)
    h = LH(tmp_path, sim=sim)
    await h.mgr.tick_once()
    s = await reopen_session(h)
    await until(h, 0, 30)
    assert h.store.pending_entries(s.id)
    await h.L.stop()
    mgr = SessionManager(h.thread, h.store, h.journal, h.bus, learning=h.L)  # the application restarts
    h.mgr = mgr
    await mgr.boot()
    assert h.store.get_session(s.id).status == "interrupted" and h.store.pending_entries(s.id)
    await until(h, 0, 50)
    await mgr.start(s.id)
    await until(h, 1, 3)
    assert h.store.trades(session_id=s.id), "restarted before the reopen: the entry is sent"
    await h.close()


async def test_an_exit_in_the_break_waits_for_the_market_without_an_alert(tmp_path):
    h = Harness(tmp_path, sim=SimBroker(seed=11, start=MON_08 + 15 * 3600 + 30 * 60, history_days=5))
    await h.mgr.tick_once()
    s = await h.mgr.create_session(SessionIn(name="G", daily_loss_pct=90, assignments=[AssignmentIn(symbol="GOLD", timeframe="H4", strategy="trend_breakout")]))
    await h.mgr.start(s.id)
    t = h.sim.tick("GOLD")
    r = h.sim.market_order("GOLD", "long", 0.01, round(t.bid - 30, 2), 0, magic=h.store.assignments(s.id)[0].magic)
    await until(h, 0, 20)
    [a] = h.store.assignments(s.id)
    [pos] = h.mgr._session_positions(s)
    calls = []
    real_close = h.sim.close
    h.sim.close = lambda *x, **k: calls.append(h.sim.now) or real_close(*x, **k)
    assert not await h.mgr._close(s, pos, "strategy exit: close below the 20-bar low", a)
    assert not kinds(h, s, "order_fail") and not [j for j in h.store.journal(session_id=s.id, limit=100) if j.alert and "Close" in j.message]
    [p] = h.store.pending_entries(s.id)
    assert p.kind == "exit" and p.ticket == r.ticket
    await until(h, 1, 2)
    assert h.positions(s) == [] and h.store.pending_entries(s.id, status=None)[0].status == "closed"
    assert len(calls) == 2, calls  # the refused try in the break and the close after the reopen: no request per pass
    h.close()


async def test_a_paper_exit_in_the_break_is_deferred_but_a_paper_stop_still_closes(tmp_path):
    h = Harness(tmp_path, sim=SimBroker(seed=11, start=MON_08 + 15 * 3600 + 30 * 60, history_days=5))
    await h.mgr.tick_once()
    s = await h.mgr.create_session(SessionIn(name="P", execution="paper", daily_loss_pct=90,
                                             assignments=[AssignmentIn(symbol="GOLD", timeframe="H4", strategy="trend_breakout")]))
    await h.mgr.start(s.id)
    [a] = h.store.assignments(s.id)
    res = await h.thread.run(lambda b: b.market_order("GOLD", "long", 0.01, round(b.tick("GOLD").bid - 30, 2), 0, a.magic, "fxc test"))
    assert res.ok and res.ticket < 0
    await until(h, 0, 20)
    [pos] = h.mgr._session_positions(s)
    assert not await h.mgr._close(s, pos, "strategy exit: test", a)  # the Paper book refuses, like the market
    assert h.store.pending_entries(s.id)[0].kind == "exit"
    await h.mgr.stop(s.id, close_positions=True)  # a Paper Session always closes its positions on stop
    assert h.mgr._session_positions(s) == []
    h.close()


@pytest.mark.parametrize("execution", ["broker", "paper"])
async def test_gold_trend_h4_through_several_breaks_matches_its_shadow_trades(tmp_path, execution):
    """A real GOLD H4 run: exits and entries on the 00:00 close go through _act, wait for the reopen,
    and every live entry lies within its Shadow Trade's bar (live fills at 01:01, shadow at 00:00)."""
    h = LH(tmp_path, sim=SimBroker(seed=8, start=MON_08, history_days=25))
    h.store.set_global_limits(__import__("fxcommand.risk", fromlist=["RiskLimits"]).RiskLimits(max_positions_global=20, daily_loss_pct_global=90))
    await h.mgr.tick_once()
    s = await h.mgr.create_session(SessionIn(
        name="Trend", execution=execution, daily_loss_pct=90,
        assignments=[AssignmentIn(symbol="GOLD", timeframe="H4", strategy="trend_breakout", params={"entry": 5, "exit": 2, "allow_short": True})],
    ))
    await h.mgr.start(s.id)
    closes = []
    real_close = h.sim.close
    h.sim.close = lambda *x, **k: closes.append(h.sim.now) or real_close(*x, **k)
    await h.bars(int(3.9 * DAY / 60))
    alerts = [j.message for j in h.store.journal(session_id=s.id, limit=10_000) if j.alert and "close it in the terminal" in j.message]
    assert alerts == []
    in_break = [t for t in closes if 23 * 60 + 59 <= (t % DAY) // 60 or (t % DAY) // 60 < 60]
    assert len(in_break) <= 1 + len(h.store.pending_entries(s.id, status=None))  # one refused try per deferred exit at most
    trades = h.store.trades(session_id=s.id)
    shadows = h.L.repo.shadow_closed("GOLD", "H4", limit=10_000) + h.L.repo.open_shadows("GOLD", "H4")
    champion = h.L.champion_of(h.store.assignments(s.id)[0]).key
    opens = sorted(r.open_ts for r in shadows if r.candidate_key == champion)
    assert trades, "no live trade in four days"
    for t in trades:
        assert any(o <= t.open_time < o + 4 * 3600 for o in opens), (t.open_time, opens)
    await h.close()
