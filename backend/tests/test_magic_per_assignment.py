"""One Magic Number per Assignment (ADR 0009): the same Symbol on two Timeframes, Paper routing of
every Assignment magic, the migration of Sessions that had one magic, and ownership that survives
an edit of the strategy."""

import pytest
from sqlmodel import Session as DB, text

from fxcommand.broker.sim import SimBroker
from fxcommand.engine import AssignmentIn, SessionIn, SessionManager

from .conftest import FAST, MON_08, Harness


def gold_twice(name="GG", execution="broker"):
    return SessionIn(
        name=name, execution=execution, daily_loss_pct=90, max_positions=5,
        assignments=[AssignmentIn(symbol="GOLD", timeframe="M1", params=FAST), AssignmentIn(symbol="GOLD", timeframe="M5", params=FAST)],
    )


def per_assignment(h, s):
    """{assignment id: [positions]} by the engine's ownership rule."""
    assigns = h.store.assignments(s.id)
    out = {a.id: [] for a in assigns}
    for p in h.mgr._session_positions(s):
        o = h.mgr._owner(s, p, assigns)
        out.setdefault(o.id if o else None, []).append(p)
    return out


async def test_two_gold_positions_at_once_each_with_its_assignments_magic(h):
    s = await h.mgr.create_session(gold_twice())
    a1, a5 = h.store.assignments(s.id)
    assert a1.magic == s.magic and a5.magic != a1.magic  # the first Assignment keeps the Session's magic
    await h.mgr.start(s.id)
    for _ in range(300):
        await h.bars(1)
        mine = h.positions(s)
        assert len(mine) <= 2 and all(len(v) <= 1 for v in per_assignment(h, s).values())
        if {p.magic for p in mine} == {a1.magic, a5.magic}:
            break
    else:
        pytest.fail("never held both GOLD positions at once")
    trades = h.store.trades(session_id=s.id)
    assert {t.assignment_id: t.magic for t in trades} == {a1.id: a1.magic, a5.id: a5.magic}
    assert all(t.timeframe == ("M1" if t.magic == a1.magic else "M5") for t in trades)
    await h.mgr.kill_all()
    assert h.positions(s) == []


async def test_every_assignment_magic_of_a_paper_session_is_routed_to_the_paper_book(tmp_path):
    h = Harness(tmp_path)
    await h.mgr.tick_once()
    sent = []
    real_order = h.sim.market_order
    h.sim.market_order = lambda *a, **k: sent.append(a) or real_order(*a, **k)  # the real Account
    s = await h.mgr.create_session(gold_twice("Paper GG", execution="paper"))
    await h.mgr.start(s.id)
    await h.bars(120)
    by_assignment = {t.assignment_id for t in h.store.trades(session_id=s.id)}
    assert by_assignment == {a.id for a in h.store.assignments(s.id)} and sent == []
    assert all(t.paper and t.ticket < 0 for t in h.store.trades(session_id=s.id))

    # an Assignment added by an edit, then an application restart: still Paper
    await h.mgr.stop(s.id)
    spec = gold_twice("Paper GG", execution="paper")
    spec.assignments.append(AssignmentIn(symbol="EURUSD", timeframe="M1", params=FAST))
    await h.mgr.update_session(s.id, spec)
    mgr = SessionManager(h.thread, h.store, h.journal, h.bus)  # a fresh engine over the same database
    h.mgr = mgr
    await mgr.boot()
    await mgr.start(s.id)
    await h.bars(60)
    eur = next(a for a in h.store.assignments(s.id) if a.symbol == "EURUSD")
    assert any(t.assignment_id == eur.id and t.paper for t in h.store.trades(session_id=s.id))
    assert sent == [] and h.sim.positions() == []  # nothing ever reached the Account
    h.close()


async def test_sessions_from_before_adr_0009_are_migrated_without_a_duplicate_entry(tmp_path):
    sim = SimBroker(seed=11, start=MON_08, history_days=5)
    h = Harness(tmp_path, sim=sim, db_name="old.db")
    await h.mgr.tick_once()
    s = await h.session("Old", symbols=("EURUSD", "GBPUSD"), daily_loss_pct=90)
    # make the database look like it did before: one magic per Session, no identities...
    with DB(h.store.engine) as db:
        db.exec(text("UPDATE assignments SET magic = 0"))
        db.exec(text("DELETE FROM magic_identities"))
        db.commit()
    # ...and two positions it opened then, both carrying the Session's magic
    for sym, pip in (("EURUSD", 0.0001), ("GBPUSD", 0.0001)):
        t = sim.tick(sym)
        assert sim.market_order(sym, "long", 0.1, round(t.bid - 60 * pip, 5), 0.0, magic=s.magic, comment="fxc old").ok
    h.close()

    h2 = Harness(tmp_path, sim=sim, db_name="old.db")  # the new version starts on the old database
    eur, gbp = h2.store.assignments(s.id)
    assert eur.magic == s.magic and gbp.magic not in (0, s.magic)
    await h2.mgr.boot()
    await h2.mgr.start(s.id)
    await h2.mgr.tick_once()
    adopted = {t.symbol: t.assignment_id for t in h2.store.trades(session_id=s.id)}
    assert adopted == {"EURUSD": eur.id, "GBPUSD": gbp.id}  # the GBPUSD one is not given to EURUSD
    for _ in range(40):
        await h2.bars(1)
        assert all(len(v) <= 1 for k, v in per_assignment(h2, s).items() if k is not None), "a second position was opened"
        assert per_assignment(h2, s).get(None, []) == []
    await h2.mgr.kill_all()
    assert h2.positions(s) == []
    h2.close()


async def test_a_position_left_open_keeps_its_owner_when_the_strategy_is_edited(h):
    s = await h.session("Edit", symbols=("EURUSD",), daily_loss_pct=90)
    await h.mgr.start(s.id)
    for _ in range(60):
        await h.bars(1)
        if h.positions(s):
            break
    [p] = h.positions(s)
    await h.mgr.stop(s.id, close_positions=False)
    spec = SessionIn(name="Edit", daily_loss_pct=90,
                     assignments=[AssignmentIn(symbol="EURUSD", timeframe="M1", strategy="donchian_breakout", params={"period": 5, "exit_period": 3})])
    await h.mgr.update_session(s.id, spec)
    [a] = h.store.assignments(s.id)
    assert a.magic != p.magic  # a new identity...
    await h.mgr.start(s.id)
    await h.mgr.tick_once()
    assert h.mgr._position_of(s, a).ticket == p.ticket  # ...still owns the position left open
    for _ in range(30):
        await h.bars(1)
        assert len(h.positions(s)) <= 1
    # and going back to the first strategy gives the first magic back
    await h.mgr.stop(s.id, close_positions=True)
    await h.mgr.update_session(s.id, SessionIn(name="Edit", daily_loss_pct=90, assignments=[AssignmentIn(symbol="EURUSD", timeframe="M1", params=FAST)]))
    assert h.store.assignments(s.id)[0].magic == p.magic


async def test_magics_are_never_reused(h):
    s = await h.mgr.create_session(gold_twice())
    used = {a.magic for a in h.store.assignments(s.id)} | {s.magic}
    await h.mgr.delete_session(s.id)
    t = await h.mgr.create_session(gold_twice("Again"))
    assert not ({a.magic for a in h.store.assignments(t.id)} | {t.magic}) & used
