"""The Cost Check at its real limit (0.15R, spec D4/D14/D26): it blocks a start whose round trip costs
too much of the stop, is enforced in the engine (no API/MCP bypass), takes a journaled override per
(Session, Symbol, Timeframe), holds at a Promotion, makes auto-resume skip, and never prices from a
quote in the daily break."""

import pytest

from fxcommand.broker.sim import DAY, SimBroker
from fxcommand.engine import AssignmentIn, DomainError, SessionIn, SessionManager
from fxcommand.learning.candidate import Candidate

from .conftest import FAST, MON_08, Harness
from .test_learning_service import LH, add_challenger


def at_default(h):
    h.store.update_app_settings({"cost_check_max_r": 0.15})


def gold_reopen():
    return AssignmentIn(symbol="GOLD", timeframe="H1", strategy="session_drift")


async def test_a_fast_m1_strategy_is_blocked_and_gold_reopen_starts(h):
    at_default(h)
    s = await h.session("M1", symbols=("EURUSD",))
    with pytest.raises(DomainError) as e:
        await h.mgr.start(s.id)
    assert e.value.code == "cost_check" and "EURUSD M1" in e.value.message and "0.15R" in e.value.message
    g = await h.mgr.create_session(SessionIn(name="Reopen", daily_loss_pct=90, assignments=[gold_reopen()]))
    [c] = await h.mgr.cost_check(g)
    assert c["allowed"] and c["cost_r"] < 0.15
    await h.mgr.start(g.id)
    assert h.store.get_session(g.id).status == "running"


async def test_an_override_lets_it_start_is_journaled_and_is_per_timeframe(h):
    at_default(h)
    s = await h.mgr.create_session(
        SessionIn(name="Both", daily_loss_pct=90,
                  assignments=[AssignmentIn(symbol="GOLD", timeframe="M1", params=FAST), AssignmentIn(symbol="GOLD", timeframe="M5", params={**FAST, "sl_atr": 1.0})])
    )
    await h.mgr.set_cost_override(s.id, "GOLD", "M1", True, "operator accepts the cost on demo")
    with pytest.raises(DomainError) as e:
        await h.mgr.start(s.id)
    assert "GOLD M5" in e.value.message and "GOLD M1" not in e.value.message  # the M1 override does not cover M5
    await h.mgr.set_cost_override(s.id, "GOLD", "M5", True)
    await h.mgr.start(s.id)
    msgs = [j.message for j in h.store.journal(session_id=s.id, limit=100)]
    assert any("Cost Check overridden for GOLD M1" in m for m in msgs) and any("override used for GOLD M5" in m for m in msgs)


async def test_the_break_never_prices_the_check(tmp_path):
    h = Harness(tmp_path, sim=SimBroker(seed=4, start=MON_08 + 16 * 3600 + 20 * 60, history_days=10))  # Tuesday 00:20, GOLD shut
    at_default(h)
    await h.mgr.tick_once()
    g = await h.mgr.create_session(SessionIn(name="Reopen", daily_loss_pct=90, assignments=[gold_reopen()]))
    [c] = await h.mgr.cost_check(g)
    assert not c["allowed"] and "market is shut" in c["reason"]
    while (h.sim.now % DAY) // 60 != 70:
        await h.bars(1)  # 01:10: open, the 70-point reopen quote is the only one known
    [c] = await h.mgr.cost_check(g)
    assert c["allowed"] and c["spread"] == pytest.approx(0.70)
    h.close()


async def test_auto_resume_skips_a_session_that_now_fails_the_check(tmp_path):
    h = Harness(tmp_path)
    await h.mgr.tick_once()
    s = await h.session("Resume", symbols=("EURUSD",), auto_resume=True)
    await h.mgr.start(s.id)  # passes at the harness's lifted limit
    at_default(h)  # ...and then the operator tightens it
    mgr = SessionManager(h.thread, h.store, h.journal, h.bus)
    await mgr.boot()
    assert h.store.get_session(s.id).status == "interrupted"
    assert any("Auto-resume failed: Cost Check failed" in j.message for j in h.store.journal(session_id=s.id, limit=50))
    h.close()


async def test_a_promotion_to_a_costlier_candidate_is_cancelled(tmp_path):
    h = LH(tmp_path)
    await h.mgr.tick_once()
    s = await h.mgr.create_session(SessionIn(name="Promo", daily_loss_pct=90, assignments=[gold_reopen()]))
    await h.mgr.start(s.id)
    at_default(h)
    scalper = Candidate.of("ema_cross", {**FAST, "sl_atr": 0.2})  # a stop a few spreads wide
    h.L.repo.ensure_candidate(scalper)
    h.L.repo.add_pending(session_id=s.id, symbol="GOLD", timeframe="H1", candidate_key=scalper.key, kind="promotion", reason="test")
    [a] = h.store.assignments(s.id)
    await h.mgr._apply_pending(h.store.get_session(s.id), a)
    assert h.store.assignments(s.id)[0].strategy == "session_drift"
    assert h.L.pending(s.id, "GOLD", "H1") is None
    assert any("cancelled — Cost Check" in j.message for j in h.store.journal(session_id=s.id, limit=50))
    await h.close()


def test_the_typical_spread_keeps_being_saved_after_the_sample_window_is_full(tmp_path):
    from fxcommand.broker.types import Tick
    from fxcommand.risk.spreads import SETTING, SpreadBook
    from fxcommand.store import Store

    store = Store(f"sqlite:///{tmp_path / 's.db'}")
    book = SpreadBook(store)
    now = MON_08
    for i in range(100):
        spread = 0.30 if i < 60 else 0.50  # the spread changes after the window of 60 is full
        book.sample("GOLD", Tick("GOLD", now, 2000.0, 2000.0 + spread, 0.01), now)
    assert store.get_setting(SETTING)["GOLD"] == pytest.approx(0.50)
    assert SpreadBook(store).typical("GOLD") == pytest.approx(0.50)  # what a restarted engine prices from
