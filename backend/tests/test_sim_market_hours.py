"""SimBroker as the GOLD strategies need it (milestone 2): GOLD's daily break and reopen spread,
swap fields, deep H1 history, and the Gold Reopen Risk Profile."""

import numpy as np
import pytest

from fxcommand.broker.sim import DAY, LEGACY_SYMBOLS, RET_MARKET_CLOSED, SimBroker
from fxcommand.broker.types import SymbolInfo, Timeframe
from fxcommand.learning.costs import CostModel
from fxcommand.store.repo import DEFAULT_PROFILES, Store
from fxcommand.store.models import RiskProfileRow
from fxcommand.strategies import STRATEGIES

MON_08 = 1_704_700_800  # 2024-01-08 08:00 server time
MON_2350 = MON_08 + 15 * 3600 + 50 * 60  # Monday 23:50


def minute_of_day(t):
    return (np.asarray(t) % DAY) // 60


@pytest.fixture
def sim():
    s = SimBroker(seed=3, start=MON_2350, history_days=5)
    s.connect()
    return s


def to(sim: SimBroker, hh: int, mm: int) -> None:
    """Step until the server clock reads hh:mm (the next occurrence)."""
    target = hh * 60 + mm
    while (sim.now % DAY) // 60 != target:
        sim.step()


# ------------------------------------------------------------------ daily break
def test_gold_prints_no_bars_in_the_daily_break_while_fx_does(sim):
    gold = sim.closed_bars("GOLD", Timeframe.M1, 5 * 1440)
    fx = sim.closed_bars("EURUSD", Timeframe.M1, 5 * 1440)
    gm = minute_of_day(gold["time"])
    assert not ((gm >= 23 * 60 + 57) | (gm < 60)).any()
    assert ((minute_of_day(fx["time"]) < 60)).any()
    to(sim, 1, 30)  # step through the break
    gm = minute_of_day(sim.closed_bars("GOLD", Timeframe.M1, 200)["time"])
    assert not ((gm >= 23 * 60 + 57) | (gm < 60)).any()
    assert set(range(60, 90)) <= set(gm.tolist())


def test_other_symbols_are_unchanged_by_golds_trading_hours():
    a = SimBroker(seed=9, start=MON_2350, history_days=3)
    b = SimBroker(seed=9, start=MON_2350, history_days=3, symbols=LEGACY_SYMBOLS)
    a.step(180), b.step(180)  # across midnight and the reopen
    for sym in ("EURUSD", "GBPUSD", "USDJPY"):
        assert a.closed_bars(sym, Timeframe.M1, 6000).equals(b.closed_bars(sym, Timeframe.M1, 6000))


def test_h1_bars_skip_the_midnight_hour_and_the_signal_bar_fires_session_drift():
    sim = SimBroker(seed=3, start=MON_2350, history_days=20)
    to(sim, 5, 0)
    h1 = sim.closed_bars("GOLD", Timeframe.H1, 400)
    assert 0 not in set((h1["time"] % DAY // 3600).tolist())
    f = STRATEGIES["session_drift"].signals(h1, {})
    fired = h1["time"][f["long"].astype(bool)]
    assert len(fired) >= 2 and set((fired % DAY // 3600).tolist()) == {23}  # the 23:00 bar, before the reopen


def test_orders_are_refused_while_gold_is_shut_and_work_after_the_reopen(sim):
    t = sim.tick("GOLD")
    r = sim.market_order("GOLD", "long", 0.01, round(t.bid - 20, 2), 0, magic=7)
    assert r.ok
    to(sim, 0, 30)
    assert not sim.market_open("GOLD") and sim.market_open("EURUSD")
    t = sim.tick("GOLD")
    assert sim.market_order("GOLD", "long", 0.01, round(t.bid - 20, 2), 0, magic=7).retcode == RET_MARKET_CLOSED
    assert sim.modify(r.ticket, round(t.bid - 25, 2), 0).retcode == RET_MARKET_CLOSED
    assert sim.close(r.ticket).retcode == RET_MARKET_CLOSED
    assert sim.state()["market_open"]["GOLD"] is False
    to(sim, 1, 1)
    assert sim.close(r.ticket).ok


def test_the_last_quote_ages_while_shut(sim):
    to(sim, 0, 40)
    t = sim.tick("GOLD")
    assert sim.now - t.time >= 40 * 60
    assert sim.tick("EURUSD").time == sim.now


def test_reopen_spread_is_wider_for_fifteen_minutes_and_needs_the_gold_reopen_profile(sim):
    caps = {p.name: p.max_spread_points for p in DEFAULT_PROFILES}
    to(sim, 1, 0)
    reopen = sim.tick("GOLD").spread_points
    assert reopen == 70
    assert reopen > caps["Gold"] and reopen <= caps["Gold Reopen"]
    to(sim, 1, 14)
    assert sim.tick("GOLD").spread_points == 70
    to(sim, 1, 15)
    assert sim.tick("GOLD").spread_points == 30


def test_stops_wait_for_the_reopen_and_fill_at_the_gap(sim):
    t = sim.tick("GOLD")
    sl = round(t.bid - 10, 2)
    r = sim.market_order("GOLD", "long", 0.01, sl, 0, magic=7)
    to(sim, 0, 20)
    sim.shock("GOLD", -0.02)  # a move while shut: no bar, no stop
    assert [p.ticket for p in sim.positions()] == [r.ticket]
    to(sim, 1, 1)
    assert sim.positions() == []
    closed = sim.history(0)[-1]
    assert closed.reason == "sl" and closed.price_close < sl - 20  # filled at the gap, not at the stop


# ------------------------------------------------------------------------ swap
def test_symbol_info_reports_swaps_and_the_cost_model_reads_them(sim):
    info = sim.symbol_info("GOLD")
    assert (info.swap_long, info.swap_short, info.swap_mode, info.swap_rollover3days) == (-86.84, 19.79, 1, 3)
    cm = CostModel.from_symbol(info, spread=0.3)
    assert cm.swap_long == -86.84 and cm.swap_short == 19.79 and cm.triple_weekday == 2  # Wednesday
    assert SymbolInfo("X", "", 2, 0.01, 0.01, 1.0, 100, 0.01, 50, 0.01, 0).swap_mode == 0  # defaults: no swap


# --------------------------------------------------------------- deep history
def test_deep_history_reaches_600_days_on_long_timeframes_without_changing_m1():
    plain = SimBroker(seed=4, start=MON_08, history_days=20)
    deep = SimBroker(seed=4, start=MON_08, history_days=20, deep_history_days=600)
    assert deep.closed_bars("EURUSD", Timeframe.M1, 20000).equals(plain.closed_bars("EURUSD", Timeframe.M1, 20000))
    assert deep.closed_bars("GOLD", Timeframe.H1, 100).equals(plain.closed_bars("GOLD", Timeframe.H1, 100))
    for tf in (Timeframe.H1, Timeframe.H4, Timeframe.D1):
        bars = deep.closed_bars("GOLD", tf, 100_000)
        assert (MON_08 - int(bars["time"].iloc[0])) / DAY >= 600
        assert bars["time"].is_monotonic_increasing and bars["time"].is_unique
        assert int(bars["time"].iloc[-1]) + tf.seconds <= deep.now
        assert (bars["high"] >= bars[["open", "close"]].max(axis=1)).all()
        assert (bars["low"] <= bars[["open", "close"]].min(axis=1)).all()
    h1 = deep.closed_bars("GOLD", Timeframe.H1, 100_000)
    assert 0 not in set((h1["time"] % DAY // 3600).tolist())
    assert (((h1["time"] // DAY + 3) % 7) < 5).all()
    # the deep path joins the M1 history without a jump
    closes = h1["close"].to_numpy()
    assert np.max(np.abs(np.diff(np.log(closes)))) < 0.05


def test_deep_history_serves_a_backtest_length_request_quickly():
    import time

    t0 = time.perf_counter()
    deep = SimBroker(seed=4, start=MON_08, history_days=60, deep_history_days=600)
    bars = deep.closed_bars("GOLD", Timeframe.H4, 5000)
    assert len(bars) > 2000 and time.perf_counter() - t0 < 10


# ------------------------------------------------------------- Risk Profiles
def test_gold_reopen_profile_reaches_existing_databases_once(tmp_path):
    url = f"sqlite:///{tmp_path / 'fx.db'}"
    store = Store(url)
    names = {p.name for p in store.risk_profiles()}
    assert "Gold Reopen" in names
    reopen = next(p for p in store.risk_profiles() if p.name == "Gold Reopen")
    assert reopen.max_spread_points == 100 and not reopen.breakeven and not reopen.trailing

    # a database from before the profile existed gets it on the next start
    from sqlmodel import Session, delete

    with Session(store.engine) as db:
        db.exec(delete(RiskProfileRow).where(RiskProfileRow.name == "Gold Reopen"))
        db.commit()
    store.set_setting("seeded_profiles", None)
    store = Store(url)
    assert "Gold Reopen" in {p.name for p in store.risk_profiles()}

    # one the operator deleted afterwards stays deleted
    with Session(store.engine) as db:
        db.exec(delete(RiskProfileRow).where(RiskProfileRow.name == "Gold Reopen"))
        db.commit()
    store = Store(url)
    assert "Gold Reopen" not in {p.name for p in store.risk_profiles()}
