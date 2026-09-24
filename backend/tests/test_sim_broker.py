import pytest

from fxcommand.broker.sim import SimBroker, weekday
from fxcommand.broker.types import Timeframe

MON_08 = 1_704_700_800  # 2024-01-08 08:00 UTC


@pytest.fixture
def sim():
    s = SimBroker(seed=7, start=MON_08, history_days=20)
    s.connect()
    return s


def test_deterministic_for_same_seed():
    a = SimBroker(seed=3, start=MON_08, history_days=5)
    b = SimBroker(seed=3, start=MON_08, history_days=5)
    a.step(30), b.step(30)
    assert a.tick("EURUSD") == b.tick("EURUSD")


def test_clock_and_closed_bars(sim):
    assert sim.server_time() == MON_08
    bars = sim.closed_bars("EURUSD", Timeframe.M1, 10)
    assert len(bars) == 10 and int(bars["time"].iloc[-1]) == MON_08 - 60
    sim.step(5)
    assert sim.server_time() == MON_08 + 300
    m5 = sim.closed_bars("EURUSD", Timeframe.M5, 3)
    assert int(m5["time"].iloc[-1]) == MON_08  # the 08:00-08:05 bar is now closed
    assert (m5["high"] >= m5[["open", "close"]].max(axis=1)).all()
    assert (m5["low"] <= m5[["open", "close"]].min(axis=1)).all()


def test_higher_timeframes_exclude_forming_bar(sim):
    sim.step(30)  # 08:30
    h1 = sim.closed_bars("GOLD", Timeframe.H1, 5)
    assert int(h1["time"].iloc[-1]) == MON_08 - 3600  # 07:00 bar; 08:00 still forming
    assert len(h1) == 5
    d1 = sim.closed_bars("GOLD", Timeframe.D1, 3)
    assert all(weekday(int(t)) < 5 for t in d1["time"])


def test_weekend_is_skipped():
    fri = MON_08 + 4 * 86400 + 15 * 3600 + 58 * 60  # Fri 23:58
    s = SimBroker(seed=1, start=fri, history_days=3)
    s.step(3)
    assert weekday(s.server_time() - 60) == 0  # last bar on Monday
    assert s.closed_bars("EURUSD", Timeframe.M1, 1)["time"].iloc[-1] == MON_08 - 8 * 3600 + 7 * 86400


def test_order_fill_and_pnl(sim):
    t = sim.tick("EURUSD")
    r = sim.market_order("EURUSD", "long", 0.10, round(t.bid - 0.0050, 5), round(t.bid + 0.0050, 5), magic=77)
    assert r.ok and r.price == t.ask
    [p] = sim.positions(magic=77)
    assert p.volume == 0.10 and p.side == "long"
    # immediately after entry the position is down by the spread: 12 pts * $1 * 0.1 lot
    assert p.profit == pytest.approx(-1.2, abs=0.01)
    assert sim.positions(magic=78) == []


def test_order_validation(sim):
    t = sim.tick("EURUSD")
    assert sim.market_order("EURUSD", "long", 0.001, t.bid - 0.01, 0, 1).retcode == 10014
    assert sim.market_order("EURUSD", "long", 0.10, t.bid + 0.001, 0, 1).retcode == 10016  # SL above price
    assert sim.market_order("EURUSD", "long", 0.10, 0, 0, 1).retcode == 10016  # no SL
    assert sim.market_order("EURUSD", "short", 0.10, t.ask - 0.001, 0, 1).retcode == 10016
    assert sim.market_order("EURUSD", "long", 50, t.bid - 0.01, 0, 1).retcode == 10019  # margin


def test_shock_hits_stop_loss(sim):
    t = sim.tick("GOLD")
    r = sim.market_order("GOLD", "long", 0.10, round(t.bid - 5.0, 2), 0, magic=5)
    assert r.ok
    bal = sim.account().balance
    sim.shock("GOLD", -0.02)  # -2% ~ -$47
    assert sim.positions(5) == []
    [c] = sim.history(0)
    assert c.reason == "sl" and c.profit < 0
    assert c.price_close < round(t.bid - 5.0, 2)  # gap: filled at the open, worse than the stop
    assert sim.account().balance == pytest.approx(bal + c.profit)


def test_short_take_profit(sim):
    t = sim.tick("EURUSD")
    sim.market_order("EURUSD", "short", 0.1, round(t.ask + 0.01, 5), round(t.bid - 0.0020, 5), magic=9)
    sim.shock("EURUSD", -0.01)
    [c] = sim.history(0)
    assert c.reason == "tp" and c.profit > 0


def test_modify_and_close(sim):
    t = sim.tick("USDJPY")
    r = sim.market_order("USDJPY", "long", 0.2, round(t.bid - 0.5, 3), 0, magic=3)
    assert sim.modify(r.ticket, round(t.bid - 0.3, 3), 0).ok
    assert sim.positions(3)[0].sl == pytest.approx(round(t.bid - 0.3, 3))
    assert not sim.modify(r.ticket, round(t.bid + 0.3, 3), 0).ok
    c = sim.close(r.ticket, "manual")
    assert c.ok and sim.positions() == []
    assert sim.history(0)[0].reason == "manual"
    assert not sim.close(r.ticket).ok


def test_usdjpy_tick_value_is_price_dependent(sim):
    info = sim.symbol_info("USDJPY")
    bid = sim.tick("USDJPY").bid
    assert info.trade_tick_value == pytest.approx(100_000 * 0.001 / bid)
