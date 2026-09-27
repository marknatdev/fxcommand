"""The Paper Account (spec D8/D19/D21/D28): one notional pool Paper Sessions size from, with its own
day start and limits, never mixed into the real Account; a guarded reset that archives by epoch and
never reuses a ticket; swap on Paper Positions; catch-up over a gap longer than the M1 history."""

from dataclasses import replace

import pytest

from fxcommand.broker.paper import PaperBook
from fxcommand.broker.sim import DAY, SimBroker
from fxcommand.engine import AssignmentIn, DomainError, SessionIn

from .conftest import FAST, MON_08, Harness, LiveSim


async def paper_session(h, name="P", symbols=("EURUSD",), **kw):
    s = await h.mgr.create_session(
        SessionIn(name=name, execution="paper", daily_loss_pct=kw.pop("daily_loss_pct", 50),
                  assignments=[AssignmentIn(symbol=x, timeframe="M1", params=FAST) for x in symbols], **kw)
    )
    await h.mgr.start(s.id)
    return s


async def first_trade(h, s, bars=120):
    for _ in range(bars):
        await h.bars(1)
        trades = h.store.trades(session_id=s.id)
        if trades:
            return trades[0]
    pytest.fail("no Paper trade")


async def test_paper_sizes_from_the_pool_and_never_touches_the_real_accounts_numbers(tmp_path):
    h = Harness(tmp_path, sim=LiveSim(seed=11, start=MON_08, history_days=5))
    h.store.set_live_enabled(h.sim.login, True)
    h.store.set_paper_account(start_balance=100_000.0)  # before the first pass records the Paper day start
    await h.mgr.tick_once()
    real = h.sim.account()
    s = await paper_session(h)
    t = await first_trade(h, s)
    assert t.paper and t.risk_amount == pytest.approx(1000.0, rel=0.05)  # 1% of the 100,000 pool, not of the real 10,000
    floor = h.mgr._ensure_floor(real)  # the live Equity Floor, set from the real equity
    await h.bars(30)
    assert len(h.store.trades(session_id=s.id)) >= 2  # Paper keeps trading on its 100,000
    ds = h.store.get_setting(f"day_start:{real.login}")
    assert ds["equity"] == pytest.approx(real.equity, abs=1)
    assert h.mgr.snapshot_account().equity == pytest.approx(real.equity, abs=1)  # self._account is the real Account
    assert h.store.equity_curve() and all(abs(e.equity - real.equity) < 1 for e in h.store.equity_curve())
    assert h.store.equity_floor(real.login) == floor and floor["floor"] == pytest.approx(0.8 * real.equity, abs=1)
    assert not h.store.equity_floor(real.login).get("breached_at")
    h.close()


async def test_a_small_real_account_does_not_stop_paper_and_the_paper_cap_stops_only_paper(tmp_path):
    h = Harness(tmp_path, sim=SimBroker(seed=11, start=MON_08, history_days=5, balance=50.0))
    await h.mgr.tick_once()
    h.store.set_paper_account(start_balance=5000.0)
    s = await paper_session(h, daily_loss_pct=3)  # 3% of the 5,000 pool, not of the real 50
    await h.bars(60)
    assert h.store.get_session(s.id).status == "running"
    assert len(h.store.trades(session_id=s.id)) >= 2
    # the Paper pool's own day loss stops the Paper Session — and only it
    real = await h.session("Real", symbols=("GBPUSD",), daily_loss_pct=90)
    await h.mgr.start(real.id)
    day = h.store.get_setting("day_start:paper")
    h.store.set_setting("day_start:paper", {**day, "equity": 10.0})  # a tiny pool day start: any loss breaches it
    for _ in range(120):
        await h.bars(1)
        if h.store.get_session(s.id).status != "running":
            break
    assert h.store.get_session(s.id).status == "stopped" and "loss" in h.store.get_session(s.id).stop_reason
    assert h.store.get_session(real.id).status == "running"
    h.close()


async def test_reset_is_guarded_archives_by_epoch_and_never_reuses_a_ticket(tmp_path):
    h = Harness(tmp_path)
    await h.mgr.tick_once()
    s = await paper_session(h)
    await first_trade(h, s)
    with pytest.raises(DomainError) as e:
        await h.mgr.reset_paper_account("RESET PAPER")
    assert e.value.code == "paper_active"
    await h.mgr.stop(s.id, close_positions=True)
    with pytest.raises(DomainError) as e:
        await h.mgr.reset_paper_account("yes")
    assert e.value.code == "confirm_required"
    old = h.store.trades(session_id=s.id)
    lowest = min(t.ticket for t in old)
    cfg = await h.mgr.reset_paper_account("RESET PAPER", 2000.0)
    assert cfg["epoch"] == 1 and h.mgr._paper_account().balance == 2000.0 and h.store.realized_paper() == 0
    await h.mgr.start(s.id)
    await h.bars(60)
    new = [t for t in h.store.trades(session_id=s.id) if t.paper_epoch == 1]
    assert new and max(t.ticket for t in new) < lowest  # tickets only go down
    assert len([t for t in h.store.trades(session_id=s.id) if t.paper_epoch == 0]) == len(old)  # archived, not deleted
    h.close()


async def test_paper_positions_pay_swap_at_each_rollover(tmp_path):
    h = Harness(tmp_path, sim=SimBroker(seed=11, start=MON_08 + 14 * 3600, history_days=5))  # Monday 22:00
    await h.mgr.tick_once()
    s = await h.mgr.create_session(SessionIn(name="Swap", execution="paper", daily_loss_pct=90,
                                             assignments=[AssignmentIn(symbol="EURUSD", timeframe="H4", strategy="trend_breakout")]))
    [a] = h.store.assignments(s.id)  # not started: no strategy acts on the position
    res = await h.thread.run(lambda b: b.market_order("EURUSD", "long", 1.0, round(b.tick("EURUSD").bid - 0.05, 5), 0, a.magic, "fxc swap"))
    assert res.ok and res.ticket < 0
    await h.bars(3 * 60)  # through Tuesday's rollover
    [pos] = h.mgr._session_positions(s)
    info = h.sim.symbol_info("EURUSD")
    one_night = -7.5 * info.point / info.trade_tick_size * info.trade_tick_value * 1.0  # -7.5 points on 1 lot
    move = (pos.price_current - pos.price_open) / info.trade_tick_size * info.trade_tick_value
    assert pos.profit == pytest.approx(move + one_night, abs=0.02)
    await h.thread.run(lambda b: b.close(res.ticket, "manual"))
    row = next(r for r in h.store.paper_closed_since(0) if r.ticket == res.ticket)
    assert row.swap == pytest.approx(one_night, abs=0.01)
    h.close()


def test_catch_up_over_a_gap_longer_than_the_m1_history_uses_h1():
    sim = SimBroker(seed=6, start=MON_08, history_days=5, deep_history_days=60)
    rows = {}

    class Repo:
        def paper_open(self):
            return [r for r in rows.values() if r.status == "open"]

        def paper_update(self, ticket, **f):
            for k, v in f.items():
                setattr(rows[ticket], k, v)

        def paper_epoch(self):
            return 0

    from fxcommand.store.models import PaperPositionRow

    h1 = sim.closed_bars("EURUSD", "H1", 2000)
    start = int(h1["time"].iloc[-40 * 24])  # ~40 days back, far beyond the 5 days of M1
    later = h1[h1["time"] > start]
    low = float(later["low"].iloc[:48].min())  # a stop the price reaches within two days
    rows[-1] = PaperPositionRow(ticket=-1, symbol="EURUSD", side="long", volume=0.1, price_open=float(h1["close"][h1["time"] == start].iloc[0]),
                                sl=low + 1e-5, tp=0.0, magic=1, time=start, checked_to=start)
    PaperBook(Repo()).sync(sim)
    r = rows[-1]
    assert r.status == "closed" and r.reason == "sl" and r.time_close <= start + 3 * DAY
