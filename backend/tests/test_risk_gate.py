import pytest

from fxcommand.broker.types import AccountInfo, Position, SymbolInfo, Tick
from fxcommand.risk import Approved, Exposure, GateInput, Rejected, RiskLimits, RiskProfile, TradingWindow, check, manage, size_volume
from fxcommand.risk.window import Blackout

MON_10 = 1_704_708_000  # 2024-01-08 10:00 UTC, a Monday

EURUSD = SymbolInfo("EURUSD", "", 5, 1e-5, 1e-5, 1.0, 100_000, 0.01, 50, 0.01, 10)
USDJPY = SymbolInfo("USDJPY", "", 3, 1e-3, 1e-3, 100_000 * 1e-3 / 150, 100_000, 0.01, 50, 0.01, 10)
GOLD = SymbolInfo("GOLD", "", 2, 0.01, 0.01, 1.0, 100, 0.01, 50, 0.01, 50)


def acct(equity=10_000.0, demo=True, trade_allowed=True):
    return AccountInfo(1, "t", "s", "c", "USD", equity, equity, 0, equity, 500, demo, trade_allowed)


def tick_for(info, bid, spread_pts):
    return Tick(info.name, MON_10, bid, round(bid + spread_pts * info.point, info.digits), info.point)


def gate(info=EURUSD, bid=1.10000, spread=10, side="long", sl_dist=0.0020, tp_dist=0.0040, **kw):
    base = dict(
        symbol=info,
        tick=tick_for(info, bid, spread),
        side=side,
        sl_dist=sl_dist,
        tp_dist=tp_dist,
        account=acct(),
        live_enabled=False,
        profile=RiskProfile(),
        session_limits=RiskLimits(),
        global_limits=RiskLimits(),
        exposure=Exposure(0, 0, 0.0, 0.0, 10_000.0),
        window=TradingWindow(),
        now=MON_10,
    )
    base.update(kw)
    return GateInput(**base)


# ------------------------------------------------------------------ sizing
@pytest.mark.parametrize(
    "info, risk, sl_dist, expected",
    [
        (EURUSD, 100, 0.0020, 0.50),  # 200 pts * $1 = $200/lot -> 0.5 lot
        (EURUSD, 100, 0.0030, 0.33),  # 0.333.. rounds DOWN
        (USDJPY, 100, 0.300, 0.50),  # 300 pts * $0.6667 = $200/lot
        (GOLD, 100, 5.00, 0.20),  # $5 on 100 oz = $500/lot
        (GOLD, 100, 7.00, 0.14),  # 0.1428 -> 0.14
        (EURUSD, 1, 0.0200, 0.0),  # below volume_min -> 0 (never round up)
    ],
)
def test_size_volume_rounds_down(info, risk, sl_dist, expected):
    assert size_volume(risk, sl_dist, info) == pytest.approx(expected)


def test_size_volume_capped_at_max():
    assert size_volume(10_000_000, 0.0010, EURUSD) == 50


# ------------------------------------------------------------------ approve
def test_long_approved_with_prices_and_risk():
    d = check(gate())
    assert isinstance(d, Approved)
    assert d.entry == pytest.approx(1.10010)  # ask
    assert d.sl == pytest.approx(1.09810)
    assert d.tp == pytest.approx(1.10410)
    assert d.volume == pytest.approx(0.50)
    assert d.risk_amount <= 100.0 + 1e-9


def test_short_uses_bid_and_sl_above():
    d = check(gate(side="short"))
    assert isinstance(d, Approved)
    assert d.entry == pytest.approx(1.10000)
    assert d.sl == pytest.approx(1.10200)
    assert d.tp == pytest.approx(1.09600)


def test_gold_sizing_through_gate():
    d = check(gate(info=GOLD, bid=2350.00, spread=30, sl_dist=5.0, tp_dist=10.0))
    assert isinstance(d, Approved)
    assert d.volume == pytest.approx(0.20)


def test_no_take_profit_when_tp_dist_zero():
    d = check(gate(tp_dist=0))
    assert isinstance(d, Approved) and d.tp == 0.0


# ------------------------------------------------------------------ reject
@pytest.mark.parametrize(
    "kw, code",
    [
        (dict(account=acct(demo=False)), "live_blocked"),
        (dict(account=acct(trade_allowed=False)), "trade_disabled"),
        (dict(now=MON_10 + 5 * 86400), "stale_quote"),  # tick from Monday, clock on Saturday
        (dict(now=MON_10 + 90, window=TradingWindow(open_day=1)), "outside_window"),
        (dict(spread=45), "spread"),
        (dict(exposure=Exposure(5, 5, 0, 0, 10_000)), "max_positions_session"),
        (dict(exposure=Exposure(0, 5, 0, 0, 10_000)), "max_positions_global"),
        (dict(exposure=Exposure(0, 0, -300, -300, 10_000)), "daily_loss_session"),
        (dict(exposure=Exposure(0, 0, -10, -300, 10_000)), "daily_loss_global"),
        (dict(sl_dist=0), "no_stop"),
        (dict(sl_dist=0.00015), "stops_level"),  # 15 pts - 10 spread = 5 < 10 stops level
        (dict(sl_dist=0.0500, profile=RiskProfile(risk_pct=0.01)), "volume_min"),
    ],
)
def test_rejections(kw, code):
    d = check(gate(**kw))
    assert isinstance(d, Rejected), d
    assert d.code == code
    assert d.reason


def test_live_allowed_when_live_enabled():
    assert isinstance(check(gate(account=acct(demo=False), live_enabled=True)), Approved)


# ------------------------------------------------------------------ window
def test_default_window():
    w = TradingWindow()
    assert w.is_open(MON_10)
    assert not w.is_open(MON_10 - 10 * 3600 + 5 * 60)  # Mon 00:05 (before open)
    assert not w.is_open(MON_10 + 4 * 86400 + 13 * 3600 + 30 * 60)  # Fri 23:30 (after close)
    assert not w.is_open(MON_10 + 86400 + 13 * 3600 + 57 * 60)  # Tue 23:57 rollover blackout
    assert not w.is_open(MON_10 + 2 * 86400 - 10 * 3600 + 5 * 60)  # Wed 00:05 blackout
    assert w.is_open(MON_10 + 2 * 86400 - 10 * 3600 + 15 * 60)  # Wed 00:15
    assert not w.is_open(MON_10 + 6 * 86400)  # Sunday


def test_window_disabled_always_open():
    assert TradingWindow(enabled=False).is_open(MON_10 + 6 * 86400)


def test_window_wraps_week():
    w = TradingWindow(open_day=6, open_time="22:00", close_day=4, close_time="21:00", blackouts=())
    assert w.is_open(MON_10 + 6 * 86400 - 10 * 3600 + 23 * 3600)  # Sunday 23:00
    assert not w.is_open(MON_10 + 5 * 86400)  # Saturday


def test_window_from_dict_roundtrip():
    w = TradingWindow(blackouts=(Blackout("12:00", "13:00"),))
    assert TradingWindow.from_dict(w.to_dict()) == w
    with pytest.raises(ValueError):
        TradingWindow.from_dict({"open_time": "25:99"})


# ------------------------------------------------------------------ manage
def pos(side="long", open_=1.10000, sl=1.09800):
    return Position(1, "EURUSD", side, 0.5, open_, open_, sl, 0.0, 0.0, 7, MON_10)


def test_manage_breakeven_after_1r():
    prof = RiskProfile(breakeven=True)
    assert manage(pos(), tick_for(EURUSD, 1.10150, 10), EURUSD, 0.001, 0.0020, prof) is None
    assert manage(pos(), tick_for(EURUSD, 1.10210, 10), EURUSD, 0.001, 0.0020, prof) == pytest.approx(1.10000)


def test_manage_trailing_only_tightens():
    prof = RiskProfile(trailing=True, trailing_atr=1.0)
    new = manage(pos(), tick_for(EURUSD, 1.10300, 10), EURUSD, 0.0010, 0.0020, prof)
    assert new == pytest.approx(1.10200)
    assert manage(pos(sl=1.10250), tick_for(EURUSD, 1.10300, 10), EURUSD, 0.0010, 0.0020, prof) is None


def test_manage_short_mirror():
    prof = RiskProfile(breakeven=True)
    p = pos(side="short", open_=1.10000, sl=1.10200)
    assert manage(p, tick_for(EURUSD, 1.09780, 10), EURUSD, 0.001, 0.0020, prof) == pytest.approx(1.10000)


def test_manage_off_by_default():
    assert manage(pos(), tick_for(EURUSD, 1.2, 10), EURUSD, 0.001, 0.002, RiskProfile()) is None


def test_manage_skips_when_tp_inside_stop_level():
    # price 5 pts from TP: a modify re-sending that TP would be rejected by the broker (stops level 10)
    p = Position(1, "EURUSD", "long", 0.5, 1.10000, 1.10300, 1.09800, 1.10305, 0.0, 7, MON_10)
    prof = RiskProfile(breakeven=True)
    assert manage(p, tick_for(EURUSD, 1.10300, 10), EURUSD, 0.001, 0.0020, prof) is None
    far = Position(1, "EURUSD", "long", 0.5, 1.10000, 1.10300, 1.09800, 1.10500, 0.0, 7, MON_10)
    assert manage(far, tick_for(EURUSD, 1.10300, 10), EURUSD, 0.001, 0.0020, prof) == pytest.approx(1.10000)
