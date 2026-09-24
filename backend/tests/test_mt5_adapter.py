"""The real ``Mt5Broker`` adapter driven through a fake ``MetaTrader5`` package (tests/fake_mt5.py):
Order Outcome classification, fill read-back, margin, terminal and account facts. No terminal, no
real order — this is how the order paths are verified without ever trading (ADR 0007)."""

import pytest

from fxcommand.broker import mt5 as mt5_module
from fxcommand.broker.mt5 import Mt5Broker

from .fake_mt5 import FakeMt5


@pytest.fixture
def fake(monkeypatch):
    f = FakeMt5()
    # the adapter looks every package attribute up on the module-level ``mt5`` name
    monkeypatch.setattr(mt5_module, "mt5", _Module(f))
    return f


class _Module:
    """Module-like view: constants from fake_mt5, functions bound to the FakeMt5 instance."""

    def __init__(self, fake: FakeMt5):
        from . import fake_mt5

        self._fake = fake
        self._consts = fake_mt5

    def __getattr__(self, name):
        if name.isupper():
            return getattr(self._consts, name)
        return getattr(self._fake, name)


@pytest.fixture
def broker(fake):
    b = Mt5Broker()
    b.connect()
    return b


def buy(b, volume=0.10):
    return b.market_order("EURUSD", "long", volume, 1.08400, 1.08700, magic=770001, comment="t")


def test_account_terminal_and_symbol_facts(broker, fake):
    a = broker.account()
    assert a.margin_mode == "hedging" and a.is_demo and a.trade_allowed
    t = broker.terminal()
    assert t.connected and t.algo_trading and t.ping_ms == 42.0 and t.build == 6182
    info = broker.symbol_info("EURUSD")
    assert info.trade_mode == "full" and info.freeze_level == 3
    fake.account.margin_mode = 0
    assert broker.account().margin_mode == "netting"
    fake.term.trade_allowed = False
    assert not broker.terminal().algo_trading and not broker.account().trade_allowed
    fake.symbol_trade_mode = 3
    assert broker.symbol_info("EURUSD").trade_mode == "closeonly"


def test_filled_entry_reports_the_position(broker, fake):
    r = buy(broker)
    assert r.ok and r.outcome == "filled"
    assert r.ticket in fake.positions and r.price == fake.ask and r.volume == 0.10
    req = fake.requests[-1]
    assert req["type_filling"] == 1  # IOC: the only mode the symbol allows
    assert req["sl"] == 1.08400 and req["magic"] == 770001


def test_price_zero_reply_is_replaced_by_position_price(broker, fake):
    fake.script = ["price_zero"]
    r = buy(broker)
    assert r.ok and r.price == fake.ask and r.volume == 0.10


def test_partial_fill_reports_real_volume(broker, fake):
    fake.script = ["partial"]
    r = buy(broker, 0.10)
    assert r.ok and r.retcode == 10010 and r.volume == 0.05


def test_order_ticket_differs_from_position_is_followed_via_deal(broker, fake):
    fake.script = ["order_differs"]
    r = buy(broker)
    assert r.ok and r.ticket in fake.positions


@pytest.mark.parametrize("what,retcode", [("requote", 10004), ("no_money", 10019), ("market_closed", 10018), ("algo_off", 10027), ("invalid_stops", 10016)])
def test_not_executed(broker, fake, what, retcode):
    fake.script = [what]
    r = buy(broker)
    assert not r.ok and r.outcome == "not_executed" and r.retcode == retcode and not fake.positions


@pytest.mark.parametrize("what", ["timeout", "connection", "error", "none", "timeout_filled"])
def test_uncertain_outcomes(broker, fake, what):
    fake.script = [what]
    r = buy(broker)
    assert not r.ok and r.outcome == "uncertain"
    assert len(fake.requests) == 1  # the adapter never retries by itself


def test_margin_required_uses_broker_calculator(broker, fake):
    assert broker.margin_required("EURUSD", "long", 0.10) == pytest.approx(0.10 * 100_000 * fake.ask / 500, abs=0.01)


def test_close_and_history(broker, fake):
    r = buy(broker)
    c = broker.close(r.ticket, "done")
    assert c.ok and not fake.positions
    req = fake.requests[-1]
    assert req["position"] == r.ticket and req["type"] == 1  # a SELL closes a BUY
    hist = broker.history(fake.now - 3600)
    assert [h.ticket for h in hist] == [r.ticket] and hist[0].reason == "expert"


def test_every_order_carries_a_stop(broker, fake):
    r = broker.market_order("EURUSD", "long", 0.1, 0.0, 0.0, magic=1)
    assert not r.ok and not fake.requests
