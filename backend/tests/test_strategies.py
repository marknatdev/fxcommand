import numpy as np
import pandas as pd
import pytest

from fxcommand.strategies import STRATEGIES, get_strategy
from fxcommand.strategies import indicators as ind


def bars_from_closes(closes, spread=0.0005):
    closes = np.asarray(closes, dtype=float)
    opens = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame(
        {
            "time": np.arange(len(closes)) * 60 + 1_704_708_000,
            "open": opens,
            "high": np.maximum(opens, closes) + spread,
            "low": np.minimum(opens, closes) - spread,
            "close": closes,
            "volume": 100.0,
        }
    )


def test_catalog_has_three_strategies_with_schemas():
    assert set(STRATEGIES) == {"ema_cross", "donchian_breakout", "rsi_reversion"}
    for s in STRATEGIES.values():
        d = s.describe()
        assert d["params"] and all({"name", "type", "default"} <= set(p) for p in d["params"])


def test_unknown_strategy():
    with pytest.raises(KeyError):
        get_strategy("nope")


def test_resolve_clamps_and_coerces():
    s = get_strategy("ema_cross")
    p = s.resolve({"fast": "5", "slow": 99999, "bogus": 1, "trend_filter": 1})
    assert p["fast"] == 5 and p["slow"] == 400 and p["trend_filter"] is True and "bogus" not in p


def test_warming_up_when_not_enough_bars():
    sig = get_strategy("ema_cross").run(bars_from_closes([1.1] * 5), {})
    assert sig.action == "none" and "warming up" in sig.reason


def test_ema_cross_long_on_upward_cross():
    closes = [1.10 - 0.0002 * i for i in range(60)] + [1.09 + 0.004 * i for i in range(1, 4)]
    bars = bars_from_closes(closes)
    s = get_strategy("ema_cross")
    # find the bar where the cross happens and evaluate up to it
    for n in range(30, len(bars) + 1):
        sig = s.run(bars.iloc[:n], {"fast": 3, "slow": 10})
        if sig.action != "none":
            break
    assert sig.action == "long"
    assert sig.sl_dist > 0 and sig.tp_dist > sig.sl_dist
    assert sig.bar_time == int(bars["time"].iloc[n - 1])


def test_ema_cross_trend_filter_blocks_counter_trend():
    closes = [1.30 - 0.001 * i for i in range(250)] + [1.05 + 0.006 * i for i in range(1, 8)]
    bars = bars_from_closes(closes)
    s = get_strategy("ema_cross")
    for n in range(210, len(bars) + 1):
        sig = s.run(bars.iloc[:n], {"fast": 3, "slow": 10, "trend_filter": True, "trend_ema": 200})
        if sig.reason != "no cross":
            break
    assert sig.action == "none" and "trend filter" in sig.reason


def test_donchian_breakout_and_exit():
    closes = [1.1000 + 0.0001 * ((i % 4) - 2) for i in range(40)] + [1.1050]
    bars = bars_from_closes(closes, spread=0.0001)
    s = get_strategy("donchian_breakout")
    assert s.run(bars, {"period": 20}).action == "long"
    down = bars_from_closes(closes[:-1] + [1.0950], spread=0.0001)
    assert s.run(down, {"period": 20}).action == "short"
    # holding a long, a close under the 10-bar low is an exit
    assert s.run(down, {"period": 20, "exit_period": 10}, "long").action == "exit"


def test_rsi_reversion_long_from_oversold_in_range():
    rng = np.random.default_rng(1)
    base = 1.1 + 0.0003 * rng.standard_normal(80)
    drop = [base[-1] - 0.0004 * i for i in range(1, 12)]
    closes = list(base) + drop + [drop[-1] + 0.0025]
    bars = bars_from_closes(closes, spread=0.0001)
    sig = get_strategy("rsi_reversion").run(bars, {"adx_max": 100})
    assert sig.action == "long", sig
    sig_block = get_strategy("rsi_reversion").run(bars, {"adx_max": 5})
    assert sig_block.action == "none" and "ADX" in sig_block.reason


def test_rsi_exit_at_mid():
    closes = [1.1 + 0.0005 * i for i in range(60)]
    sig = get_strategy("rsi_reversion").run(bars_from_closes(closes), {}, "long")
    assert sig.action == "exit"


def test_indicators_sane():
    bars = bars_from_closes(np.linspace(1.0, 1.1, 100))
    assert ind.rsi(bars["close"]).iloc[-1] > 90
    assert ind.atr(bars).iloc[-1] > 0
    assert 0 <= ind.adx(bars).iloc[-1] <= 100
    flat = bars_from_closes([1.0] * 50)
    assert ind.rsi(flat["close"]).iloc[-1] == 50
