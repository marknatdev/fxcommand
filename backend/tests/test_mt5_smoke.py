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
        assert list(bars.columns) == ["time", "open", "high", "low", "close", "volume", "spread"]
        assert bars["time"].is_monotonic_increasing
        # the forming bar is excluded: the newest closed bar has ended by the server clock
        assert int(bars["time"].iloc[-1]) + tf.seconds <= mt5_broker.server_time() + 1
    print(f"closed bars OK; last M15 bar {time.strftime('%Y-%m-%d %H:%M', time.gmtime(int(mt5_broker.closed_bars(sample, Timeframe.M15, 1)['time'].iloc[-1])))} server time")


def test_history_reads_in_pages(mt5_broker):
    """The Evidence Run reads history newest page first (``offset``); pages must stitch without gaps."""
    names = mt5_broker.symbols()
    sample = next((n for n in names if n.upper().startswith("GOLD")), names[0])
    whole = mt5_broker.closed_bars(sample, Timeframe.H4, 3000)
    pages = [mt5_broker.closed_bars(sample, Timeframe.H4, 1000, offset) for offset in (0, 1000, 2000)]
    stitched = [int(t) for p in pages[::-1] for t in p["time"]]
    assert stitched == [int(t) for t in whole["time"]], "paged H4 history differs from one read"
    print(f"\n{sample} H4: {len(whole)} bars read in 3 pages, oldest {time.strftime('%Y-%m-%d', time.gmtime(stitched[0]))}")


def test_gold_reopen_timing_and_spread(mt5_broker):
    """READ-ONLY probe of what GOLD Reopen Drift and the Pending Entries rely on (spec: MT5 smoke), from
    the recent history so it runs any day: the first M1 bar and tick after the daily break, that no H1
    bar exists for 00:00 (the first is 01:00) and the D1 bar is stamped 00:00, and the spread per minute
    from 01:00 to 01:15 (the reopen spread the Gold Reopen profile's 100-point cap allows for)."""
    import datetime as dt

    import MetaTrader5 as mt5

    names = mt5_broker.symbols()
    sym = next((n for n in names if n.upper().startswith("GOLD")), None)
    if sym is None:
        pytest.skip("no GOLD symbol in Market Watch")
    info = mt5_broker.symbol_info(sym)
    utc = dt.timezone.utc
    last = int(mt5_broker.closed_bars(sym, Timeframe.D1, 1)["time"].iloc[-1])
    days = [d for d in (last - k * 86400 for k in range(0, 10)) if (d // 86400 + 3) % 7 < 5 and (d // 86400 + 3) % 7 != 0][:4]  # Tue-Fri reopens
    assert days, "no recent weekday"
    print(f"\n{sym} daily reopen (server time), last {len(days)} trading days:")
    for day in days:
        at = lambda secs: dt.datetime.fromtimestamp(day + secs, utc)  # noqa: E731
        m1 = mt5.copy_rates_range(sym, mt5.TIMEFRAME_M1, at(-10 * 60), at(3600 + 20 * 60))
        h1 = mt5.copy_rates_range(sym, mt5.TIMEFRAME_H1, at(-3600), at(3 * 3600))
        d1 = mt5.copy_rates_range(sym, mt5.TIMEFRAME_D1, at(0), at(3600))
        ticks = mt5.copy_ticks_range(sym, at(55 * 60), at(3600 + 16 * 60), mt5.COPY_TICKS_ALL)
        if m1 is None or len(m1) == 0:
            continue
        after = [r for r in m1 if int(r["time"]) >= day]
        first_bar = int(after[0]["time"]) - day if after else None
        h1_times = sorted({int(t) - day for t in h1["time"]}) if h1 is not None else []
        first_tick = next((int(t["time"]) - day for t in ticks if int(t["time"]) >= day), None) if ticks is not None else None
        spreads = [int(r["spread"]) for r in after if 3600 <= int(r["time"]) - day < 3600 + 15 * 60]  # the bar column: a per-bar summary
        per_min: dict[int, list[float]] = {}  # the real spread: ask - bid of every tick, per minute after 01:00
        for t in ticks if ticks is not None else ():
            m = (int(t["time"]) - day - 3600) // 60
            if 0 <= m < 15 and t["ask"] > 0 and t["bid"] > 0:
                per_min.setdefault(m, []).append((float(t["ask"]) - float(t["bid"])) / info.point)
        tick_spread = [(round(max(v)), round(sorted(v)[len(v) // 2])) for _, v in sorted(per_min.items())]
        label = dt.datetime.fromtimestamp(day, utc).strftime("%a %Y-%m-%d")
        print(f"  {label}: first M1 bar {first_bar // 60 if first_bar is not None else '-'} min after 00:00, first tick {first_tick}s, "
              f"H1 bars at {[t // 3600 for t in h1_times if t >= 0]}h, D1 {'00:00' if d1 is not None and len(d1) and int(d1[0]['time']) == day else '?'}, "
              f"\n      spread 01:00-01:15 per minute from ticks (max, median points) {tick_spread}\n      bar spread column {spreads}")
        assert first_bar is not None and first_bar >= 55 * 60, "a GOLD bar inside the daily break"
        assert 0 not in h1_times, "an H1 bar stamped 00:00: the break is not where the engine expects it"
        assert d1 is None or len(d1) == 0 or int(d1[0]["time"]) == day
    print(f"  (the Gold Reopen profile allows 100 points; the terminal quotes {mt5.symbol_info(sym).spread} points now, point {info.point})")


def test_btcusd_costs_count_every_night(mt5_broker):
    """BTCUSD (spec-btc-strategies): the terminal reports no valid triple-swap day, so the Cost Model
    charges swap every night; its profile floors each bar at the spread it recorded. Read-only."""
    from fxcommand.learning.costs import CostModel

    if "BTCUSD" not in mt5_broker.symbols():
        pytest.skip("no BTCUSD on this terminal")
    info, tick = mt5_broker.symbol_info("BTCUSD"), mt5_broker.tick("BTCUSD")
    cm = CostModel.from_symbol(info, tick.ask - tick.bid, ref_price=tick.bid)
    print(f"BTCUSD swap_rollover3days={info.swap_rollover3days} every night={cm.swap_every_night} floor={cm.bar_floor} spread={tick.ask - tick.bid:.2f}")
    assert cm.swap_every_night == (not 0 <= info.swap_rollover3days <= 6) and cm.bar_floor == 1.0
    bars = mt5_broker.closed_bars("BTCUSD", Timeframe.H4, 20)
    assert (bars["spread"] > 0).all()


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
    store.update_app_settings({"cost_check_max_r": 10.0})  # the FAST M1 smoke strategy fails the real Cost Check by design
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
        # MT5 has no server clock (server time is the latest tick), so a closed market looks fresh:
        # wait for the EURUSD quote to move instead
        first = (await thread.run(lambda b: b.tick("EURUSD"))).time
        for _ in range(90):
            if (await thread.run(lambda b: b.tick("EURUSD"))).time != first:
                break
            await asyncio.sleep(1.0)
        else:
            pytest.skip("market closed: the EURUSD quote did not move for 90 s")
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
        magics = store.session_magics(s.id)
        assert not [p for p in real.positions() if p.magic in magics]  # nothing on the real Account, under any Assignment magic
    finally:
        thread.shutdown()
