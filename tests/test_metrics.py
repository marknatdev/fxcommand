"""T8: metrics in R, the daily R series and the money view, checked by hand."""

from datetime import date

import numpy as np
import pytest
import test_engine_golden as golden

from vibedesk.blocks.compile import compile_signals
from vibedesk.blocks.spec import expand_grid, validate
from vibedesk.engine.metrics import metrics
from vibedesk.engine.simulate import TRADE_DTYPE, Trades, simulate
from vibedesk.market.bars import Bars, Timeframe
from vibedesk.market.server_time import DAY, server_offset
from vibedesk.market.synthetic import SyntheticBarSource


@pytest.fixture(scope="module")
def golden_run():
    o, h, low, c = zip(*golden.OHLC, strict=True)
    n = len(o)
    bars = Bars("GOLD", Timeframe.M15, 0.01, 2, [1704067200 + 900 * i for i in range(n)],
                o, h, low, c, golden.SPREAD)  # fmt: skip
    [combination] = expand_grid(validate(golden.SPEC))
    return bars, simulate(bars, compile_signals(combination, bars), golden.COST)


def test_metrics_of_the_golden_trades(golden_run):
    bars, trades = golden_run
    atr36 = golden.ATR36
    wins = 1.995 + 2.78 / atr36
    losses = 1.005 + 1.005 + 4.22 / 3.5

    m = metrics(trades, bars)

    assert m.trades == 5
    assert m.net_r == pytest.approx(wins - losses, abs=1e-9)
    assert m.profit_factor == pytest.approx(wins / losses, abs=1e-9)
    assert m.win_rate == pytest.approx(0.4)
    assert m.expectancy_r == pytest.approx((wins - losses) / 5, abs=1e-9)
    assert m.top5_share == pytest.approx(1.0)  # only five trades
    assert m.max_drawdown_r == pytest.approx(losses, abs=1e-9)  # three losses after the first win

    # 1% risk per trade on $1,000, compounding.
    after = [1000.0]
    for r in (1.995, -1.005, -1.005, -4.22 / 3.5, 2.78 / atr36):
        after.append(after[-1] * (1 + 0.01 * r))
    assert m.equity.tolist() == pytest.approx(after, abs=1e-9)
    assert m.max_drawdown_money == pytest.approx(after[1] - after[4], abs=1e-9)
    assert m.max_drawdown_pct == pytest.approx((after[1] - after[4]) / after[1], abs=1e-12)

    # All 40 bars fall on one server day (Monday 2024-01-01).
    assert len(m.daily_r) == 1
    assert m.daily_r[0] == pytest.approx(m.net_r, abs=1e-12)


def make_trades(r, exit_time):
    records = np.zeros(len(r), dtype=TRADE_DTYPE)
    records["r"] = r
    records["exit_time"] = exit_time
    return Trades(records)


def week_of_d1_bars():
    # Server midnights Monday 2026-01-12 to Friday 2026-01-16: 22:00 UTC the evening before.
    starts = np.array([1768168800 + DAY * i for i in range(5)])
    flat = np.full(5, 100.0)
    return Bars("GOLD", Timeframe.D1, 0.01, 2, starts, flat, flat, flat, flat, np.zeros(5))


def test_top_five_share_and_profit_factor():
    bars = week_of_d1_bars()
    r = [3.0, -1.0, 2.0, 1.0, -2.0, 1.0, 1.0]

    m = metrics(make_trades(r, [bars.time[2]] * len(r)), bars)

    assert m.net_r == pytest.approx(5.0)
    assert m.top5_share == pytest.approx((3 + 2 + 1 + 1 + 1) / 5)
    assert m.profit_factor == pytest.approx(8 / 3)
    assert m.win_rate == pytest.approx(5 / 7)


def test_daily_series_has_zero_days_and_sums_to_net():
    bars = week_of_d1_bars()
    trades = make_trades([1.5, -1.0, 2.0], [bars.time[0], bars.time[3], bars.time[3]])

    m = metrics(trades, bars)

    assert m.day_start.tolist() == bars.time.tolist()  # D1 bars open at server midnight
    assert m.daily_r.tolist() == pytest.approx([1.5, 0.0, 0.0, 1.0, 0.0])
    assert m.daily_r.sum() == pytest.approx(m.net_r)


def test_no_trades():
    bars = week_of_d1_bars()

    m = metrics(make_trades([], []), bars)

    assert m.trades == 0
    assert np.isnan(m.profit_factor) and np.isnan(m.win_rate) and np.isnan(m.top5_share)
    assert m.max_drawdown_r == 0
    assert m.equity.tolist() == [1000.0]
    assert m.daily_r.tolist() == [0.0] * 5


def test_only_winners_have_infinite_profit_factor():
    bars = week_of_d1_bars()

    assert metrics(make_trades([1.0, 2.0], [bars.time[1]] * 2), bars).profit_factor == np.inf


@pytest.mark.parametrize("seed", range(5))
def test_daily_series_covers_every_trading_day_on_synthetic_data(seed):
    bars = SyntheticBarSource(seed, date(2024, 3, 4), date(2024, 3, 30)).fetch(
        "GOLD", Timeframe.M15
    )
    spec = validate(
        {
            "name": "daily",
            "symbol": "GOLD",
            "timeframe": "M15",
            "direction": "both",
            "entry": {"block": "donchian_breakout", "params": {"period": 20}},
            "exit": {"stop": {"block": "atr_stop", "params": {}}},
            "sizing": {"block": "fixed_risk", "params": {}},
        }
    )
    [combination] = expand_grid(spec)
    trades = simulate(bars, compile_signals(combination, bars), golden.COST)

    m = metrics(trades, bars)

    server_days = np.unique((bars.time + server_offset(bars.time)) // DAY)
    assert len(m.daily_r) == len(server_days) == 20  # four weeks of Monday to Friday
    assert m.daily_r.sum() == pytest.approx(m.net_r, abs=1e-9)
