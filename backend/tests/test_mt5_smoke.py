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
