"""READ-ONLY smoke test against the real MetaTrader 5 terminal. Opt-in: ``uv run pytest -m mt5``.

It connects to the terminal that is already logged in and reads account, symbols,
ticks, bars, positions and history. It never sends, modifies or closes an order:
the adapter's order methods are replaced with a tripwire for the duration of the test.
"""

import time

import pytest

from fxcommand.broker import BrokerError, Timeframe

pytestmark = pytest.mark.mt5


@pytest.fixture(scope="module")
def mt5_broker():
    mt5_mod = pytest.importorskip("MetaTrader5")
    from fxcommand.broker.mt5 import Mt5Broker

    b = Mt5Broker()

    def tripwire(*a, **k):
        raise AssertionError("smoke test must never trade")

    b.market_order = b.modify = b.close = tripwire  # type: ignore[method-assign]
    try:
        b.connect()
    except BrokerError as e:
        pytest.skip(f"MT5 terminal not available: {e}")
    yield b
    b.disconnect()
    assert mt5_mod is not None


def test_account(mt5_broker):
    a = mt5_broker.account()
    print(f"\naccount {a.login} @ {a.server} ({'DEMO' if a.is_demo else 'LIVE'}) {a.currency} balance={a.balance} equity={a.equity} leverage=1:{a.leverage} trade_allowed={a.trade_allowed}")
    assert a.login > 0 and a.currency and a.leverage > 0


def test_symbols_ticks_and_bars(mt5_broker):
    names = mt5_broker.symbols()
    print(f"\nmarket watch: {len(names)} symbols, e.g. {names[:8]}")
    assert names, "Market Watch is empty"
    sample = next((n for n in names if n.upper().startswith("EURUSD")), names[0])
    info = mt5_broker.symbol_info(sample)
    tick = mt5_broker.tick(sample)
    print(
        f"{sample}: digits={info.digits} point={info.point} tick_value={info.trade_tick_value} contract={info.contract_size} "
        f"vol {info.volume_min}/{info.volume_step}/{info.volume_max} stops_level={info.stops_level} filling={info.filling_mode} "
        f"bid={tick.bid} ask={tick.ask} spread={tick.spread_points}"
    )
    assert info.trade_tick_size > 0 and info.volume_step > 0
    assert tick.ask >= tick.bid > 0
    print(f"quote age vs server clock: {mt5_broker.server_time() - tick.time}s (Risk Gate refuses > 120s)")
    for tf in (Timeframe.M1, Timeframe.M15, Timeframe.H1):
        bars = mt5_broker.closed_bars(sample, tf, 50)
        assert len(bars) > 0, f"no {tf.value} bars"
        assert list(bars.columns) == ["time", "open", "high", "low", "close", "volume"]
        assert bars["time"].is_monotonic_increasing
        # the forming bar is excluded: the newest closed bar has ended by the server clock
        assert int(bars["time"].iloc[-1]) + tf.seconds <= mt5_broker.server_time() + 1
    print(f"closed bars OK; last M15 bar {time.strftime('%Y-%m-%d %H:%M', time.gmtime(int(mt5_broker.closed_bars(sample, Timeframe.M15, 1)['time'].iloc[-1])))} server time")


def test_positions_and_history_read(mt5_broker):
    pos = mt5_broker.positions()
    hist = mt5_broker.history(int(time.time()) - 7 * 86400)
    print(f"\nopen positions: {len(pos)}; closed in last 7 days: {len(hist)}; server time {time.strftime('%Y-%m-%d %H:%M', time.gmtime(mt5_broker.server_time()))}")
    assert isinstance(pos, list) and isinstance(hist, list)


def test_live_readiness_facts(mt5_broker):
    """Hedging account, terminal state, and our loss-at-stop math against the terminal's own
    calculators (order_calc_profit / order_calc_margin) — calculators only, nothing is sent."""
    import MetaTrader5 as mt5

    from fxcommand.risk.gate import loss_at_stop

    a = mt5_broker.account()
    t = mt5_broker.terminal()
    print(f"\nmargin mode {a.margin_mode}; terminal build {t.build}, ping {t.ping_ms} ms, algo trading {'on' if t.algo_trading else 'OFF'}")
    assert a.margin_mode == "hedging", "FXCommand trades hedging accounts only (ADR 0006)"
    names = mt5_broker.symbols()
    checked = 0
    for sym in [n for n in ("EURUSD", "USDJPY", "GBPJPY", "GOLD") if n in names] or names[:3]:
        info, tick = mt5_broker.symbol_info(sym), mt5_broker.tick(sym)
        dist = 200 * info.point
        ours = loss_at_stop(info.volume_min, dist, info)
        theirs = -mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, sym, info.volume_min, tick.ask, tick.ask - dist)
        margin = mt5_broker.margin_required(sym, "long", info.volume_min)
        print(f"{sym}: trade_mode={info.trade_mode} freeze={info.freeze_level} loss@200pts ours={ours:.4f} terminal={theirs:.4f} margin(min lot)={margin}")
        assert ours == pytest.approx(theirs, rel=0.02, abs=0.011)
        assert margin > 0
        checked += 1
    assert checked


async def test_paper_session_on_the_real_feed(tmp_path):
    """A Paper Session runs the full engine on the real terminal's prices until it has opened a paper
    position, then stops (which closes it). The real adapter's order methods are tripwired: any
    attempt to reach the Account fails the test. Takes up to ~8 minutes (M1 bar closes)."""
    import asyncio

    pytest.importorskip("MetaTrader5")
    from fxcommand.broker import BrokerThread
    from fxcommand.broker.mt5 import Mt5Broker
    from fxcommand.broker.paper import PaperBook, PaperRouter
    from fxcommand.engine import AssignmentIn, SessionIn, SessionManager
    from fxcommand.journal import EventBus, Journal
    from fxcommand.store import Store

    real = Mt5Broker()
    hits: list[str] = []

    def tripwire(name):
        def f(*a, **k):
            hits.append(name)
            raise AssertionError(f"{name} reached the real Account from a Paper Session")

        return f

    real.market_order, real.modify, real.close = tripwire("market_order"), tripwire("modify"), tripwire("close")  # type: ignore[method-assign]
    try:
        real.connect()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"MT5 terminal not available: {e}")
    store = Store(f"sqlite:///{tmp_path / 'paper.db'}")
    # a small real account cannot buy the minimum lot at 1% risk: allow it, as the runbook says for Stage 1
    prof = store.risk_profiles()[0]
    prof.allow_min_lot, prof.min_lot_max_risk_pct, prof.max_spread_points = True, 25.0, 60.0
    store.save_risk_profile(prof)
    thread = BrokerThread(PaperRouter(real, PaperBook(store)))
    bus = EventBus()
    bus.attach(asyncio.get_running_loop())
    mgr = SessionManager(thread, store, Journal(store, bus), bus)
    try:
        await mgr.tick_once()
        if mgr.now - (await thread.run(lambda b: b.tick("EURUSD"))).time > 120:
            pytest.skip("market closed: no fresh EURUSD quotes")
        fast = {"fast": 2, "slow": 3, "atr_period": 5, "sl_atr": 3.0, "tp_atr": 6.0}
        names = await thread.run(lambda b: b.symbols())
        symbols = [x for x in ("EURUSD", "GBPUSD", "USDJPY") if x in names]  # three chances per bar close
        s = await mgr.create_session(
            SessionIn(name="Paper smoke", execution="paper", daily_loss_pct=50,
                      assignments=[AssignmentIn(symbol=sym, timeframe="M1", params=fast) for sym in symbols])
        )
        await mgr.start(s.id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 480
        while loop.time() < deadline and not store.trades(session_id=s.id):
            await mgr.tick_once()
            st = next(iter(mgr.assignment_states(s.id).values()))
            assert not st["status"].startswith("error"), st
            await asyncio.sleep(1.0)
        kinds = [(j.kind, j.message[:90]) for j in store.journal(session_id=s.id, limit=500)][::-1]
        assert store.trades(session_id=s.id), f"no paper order within 8 minutes; journal: {kinds}"
        await mgr.tick_once()
        await mgr.stop(s.id)  # a Paper Session always closes its positions
        trades = store.trades(session_id=s.id)
        print(f"\npaper smoke journal: {kinds}")
        for t in trades:
            print(f"paper trade #{t.ticket} {t.side} {t.volume} @ {t.open_price} -> {t.close_price} ({t.close_reason}) P&L {t.profit}")
        assert not hits
        assert all(t.paper and t.ticket < 0 for t in trades)
        assert all(t.status == "closed" and t.profit is not None and t.close_price for t in trades)
        assert not store.paper_open()
        assert not [p for p in real.positions() if p.magic == s.magic]  # nothing on the real Account
    finally:
        thread.shutdown()
