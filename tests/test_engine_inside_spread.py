"""A stop closer than the spread is breached the moment the trade fills (ADR 0004).

Five GOLD M15 bars: three flat bars of range 2.00 (ATR(3) = 2.00), a breakout, then the entry.
atr_stop(mult=0.2) puts the stop 0.40 from the fill, but the Spread Floor is 55 points (0.55).
The exit must be at the market, -(spread + 2 * slippage) / risk, not a tidy -1R at the stop.
"""

import pytest

from vibedesk.blocks.compile import compile_signals
from vibedesk.blocks.spec import expand_grid, validate
from vibedesk.engine.costs import CostSnapshot
from vibedesk.engine.simulate import ExitReason, simulate
from vibedesk.market.bars import Bars, Timeframe

FLAT = (100.0, 101.0, 99.0, 100.0)
CASES = {
    # Long: buy at 101.6 + 0.55 + 0.01 = 102.16, stop 101.76, but the bid is only 101.60.
    # Sell at the bid open: 101.60 - 0.01 = 101.59.
    "long": dict(
        ohlc=[FLAT, FLAT, FLAT, (100.0, 102.0, 100.0, 101.5), (101.6, 102.0, 101.0, 101.5)],
        entry=102.16,
        stop=101.76,
        exit=101.59,
    ),
    # Short: sell at 98.4 - 0.01 = 98.39, stop 98.79, but the ask is already 98.4 + 0.55 = 98.95.
    # Buy back at the ask open: 98.95 + 0.01 = 98.96.
    "short": dict(
        ohlc=[FLAT, FLAT, FLAT, (100.0, 100.0, 98.0, 98.5), (98.4, 99.0, 97.5, 98.0)],
        entry=98.39,
        stop=98.79,
        exit=98.96,
    ),
}


@pytest.mark.parametrize("direction", CASES)
def test_stop_inside_the_spread_exits_at_the_market_on_the_entry_bar(direction):
    case = CASES[direction]
    o, h, low, c = zip(*case["ohlc"], strict=True)
    bars = Bars(
        symbol="GOLD",
        timeframe=Timeframe.M15,
        point=0.01,
        digits=2,
        time=[1704067200 + 900 * i for i in range(5)],
        open=o,
        high=h,
        low=low,
        close=c,
        spread=[5] * 5,
    )
    spec = validate(
        {
            "name": "inside-spread",
            "symbol": "GOLD",
            "timeframe": "M15",
            "direction": direction,
            "entry": {"block": "donchian_breakout", "params": {"period": 3}},
            "exit": {"stop": {"block": "atr_stop", "params": {"period": 3, "mult": 0.2}}},
            "sizing": {"block": "fixed_risk", "params": {}},
        }
    )
    [combination] = expand_grid(spec)
    cost = CostSnapshot(point=0.01, spread_floor_points=55, slippage_points=1)

    [trade] = simulate(bars, compile_signals(combination, bars), cost).as_dicts()

    assert trade["entry_idx"] == trade["exit_idx"] == 4
    assert trade["exit_reason"] is ExitReason.STOP_GAP
    assert trade["entry_price"] == pytest.approx(case["entry"], abs=1e-9)
    assert trade["stop_price"] == pytest.approx(case["stop"], abs=1e-9)
    assert trade["exit_price"] == pytest.approx(case["exit"], abs=1e-9)
    assert trade["risk"] == pytest.approx(0.4, abs=1e-9)
    assert trade["r"] == pytest.approx(-(0.55 + 2 * 0.01) / 0.4, abs=1e-9)  # -1.425R, not -1.025R
