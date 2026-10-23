"""A 40-bar hand-built GOLD M15 series whose every trade was worked out by hand.

Every bar's range is 2.00 and contains the previous close, so the true range is 2.00 and
ATR(3) stays exactly 2.00, except around bars 14 and 16 where ATR is computed in the comments.
Bid prices; point 0.01; Spread Floor 10 points (0.10); slippage 1 point (0.01) per side.
Every bar spread is 5 points (below the floor) except bar 7, which is 30 points.

Spec: donchian_breakout(period=3), atr_stop(period=3, mult=1.0), r_target(r=2.0), both sides.
"""

import numpy as np
import pytest

from vibedesk.blocks.compile import compile_signals
from vibedesk.blocks.spec import expand_grid, validate
from vibedesk.engine.costs import CostSnapshot
from vibedesk.engine.simulate import ExitReason, Signals, simulate
from vibedesk.market.bars import Bars, Timeframe

QUIET = (103.5, 104.5, 102.5, 103.5)
OHLC = [
    (100.0, 101.0, 99.0, 100.0),  # 0
    (100.0, 101.0, 99.0, 100.0),  # 1
    (100.0, 101.0, 99.0, 100.0),  # 2
    (100.0, 102.0, 100.0, 101.5),  # 3  close 101.5 > max high of bars 0-2 (101): long signal
    (101.6, 103.0, 101.0, 102.8),  # 4  trade 1 enters long at the open
    (102.9, 104.5, 102.5, 104.3),  # 5
    (104.4, 106.0, 104.0, 105.0),  # 6  high reaches the target; close 105 > 104.5: long signal
    (105.1, 106.0, 104.0, 104.2),  # 7  trade 2 enters long; spread 30 points
    (104.1, 105.0, 103.0, 103.5),  # 8  low reaches the stop
    QUIET,  # 9
    QUIET,  # 10
    QUIET,  # 11
    (103.4, 103.5, 101.5, 102.0),  # 12 close 102 < min low of bars 9-11 (102.5): short signal
    (101.9, 102.5, 100.5, 101.0),  # 13 trade 3 enters short
    (101.0, 104.0, 97.5, 99.0),  # 14 ask touches stop and target in one bar; short signal again
    (98.9, 100.0, 98.0, 99.5),  # 15 trade 4 enters short; ATR(14) = (2*2 + 6.5) / 3 = 3.5
    (103.0, 104.0, 102.5, 103.5),  # 16 opens above the stop: gap
    *[QUIET] * 19,  # 17-35; ATR(16) = 3.5, then decays toward 2.0 by a factor 2/3 per bar
    (103.6, 105.5, 103.5, 105.0),  # 36 close 105 > 104.5: long signal
    (105.1, 106.5, 104.5, 106.0),  # 37 trade 5 enters long
    (106.0, 107.5, 105.5, 107.0),  # 38
    (107.0, 108.5, 106.5, 108.0),  # 39 last bar: trade 5 closes at the end
]
SPREAD = [30 if i == 7 else 5 for i in range(len(OHLC))]
COST = CostSnapshot(point=0.01, spread_floor_points=10, slippage_points=1)

SPEC = {
    "name": "golden-donchian",
    "symbol": "GOLD",
    "timeframe": "M15",
    "direction": "both",
    "entry": {"block": "donchian_breakout", "params": {"period": 3}},
    "exit": {
        "stop": {"block": "atr_stop", "params": {"period": 3, "mult": 1.0}},
        "target": {"block": "r_target", "params": {"r": 2.0}},
    },
    "sizing": {"block": "fixed_risk", "params": {"risk_pct": 1.0}},
}

ATR36 = 2 + 1.5 * (2 / 3) ** 20

EXPECTED = [
    # Trade 1: long at 101.6 + 0.10 spread + 0.01 slip = 101.71; stop 99.71; target 105.71.
    # Bar 6 high 106 >= target: exit 105.71 - 0.01 = 105.70. R = 3.99 / 2 = 1.995.
    dict(side=1, entry_idx=4, exit_idx=6, entry_price=101.71, exit_price=105.70,
         stop_price=99.71, target_price=105.71, risk=2.0, r=1.995, cost_r=0.12 / 2,
         exit_reason=ExitReason.TARGET),
    # Trade 2: bar 7 spread 30 points beats the floor: entry 105.1 + 0.30 + 0.01 = 105.41.
    # Bar 8 low 103.0 <= stop 103.41: exit 103.40. R = -2.01 / 2 = -1.005.
    dict(side=1, entry_idx=7, exit_idx=8, entry_price=105.41, exit_price=103.40,
         stop_price=103.41, target_price=109.41, risk=2.0, r=-1.005, cost_r=0.32 / 2,
         exit_reason=ExitReason.STOP),
    # Trade 3: short at the bid 101.9 - 0.01 = 101.89; stop 103.89; target 97.89 (ask side).
    # Bar 14 ask high 104.1 >= stop and ask low 97.6 <= target: the stop wins. Exit 103.90.
    dict(side=-1, entry_idx=13, exit_idx=14, entry_price=101.89, exit_price=103.90,
         stop_price=103.89, target_price=97.89, risk=2.0, r=-1.005, cost_r=0.12 / 2,
         exit_reason=ExitReason.STOP),
    # Trade 4: ATR(14) = 3.5. Short at 98.89; stop 102.39. Bar 16 ask opens at 103.10 above
    # the stop: exit 103.10 + 0.01 = 103.11. R = -4.22 / 3.5.
    dict(side=-1, entry_idx=15, exit_idx=16, entry_price=98.89, exit_price=103.11,
         stop_price=102.39, target_price=91.89, risk=3.5, r=-4.22 / 3.5, cost_r=0.12 / 3.5,
         exit_reason=ExitReason.STOP_GAP),
    # Trade 5: ATR(36) = 2 + 1.5 * (2/3)**20. Long at 105.21; never reaches stop or target.
    # Closed at bar 39's bid close 108.0 - 0.01 = 107.99. R = 2.78 / ATR(36).
    dict(side=1, entry_idx=37, exit_idx=39, entry_price=105.21, exit_price=107.99,
         stop_price=105.21 - ATR36, target_price=105.21 + 2 * ATR36, risk=ATR36,
         r=2.78 / ATR36, cost_r=0.12 / ATR36, exit_reason=ExitReason.END),
]  # fmt: skip


@pytest.fixture(scope="module")
def bars():
    o, h, low, c = zip(*OHLC, strict=True)
    start = 1704067200  # 2024-01-01 00:00 UTC, a Monday
    return Bars(
        symbol="GOLD",
        timeframe=Timeframe.M15,
        point=0.01,
        digits=2,
        time=[start + 900 * i for i in range(len(OHLC))],
        open=o,
        high=h,
        low=low,
        close=c,
        spread=SPREAD,
    )


@pytest.fixture(scope="module")
def trades(bars):
    [combination] = expand_grid(validate(SPEC))
    return simulate(bars, compile_signals(combination, bars), COST)


def test_trade_count_matches(trades):
    assert len(trades) == len(EXPECTED)


@pytest.mark.parametrize("number", range(len(EXPECTED)))
def test_trade_matches_the_hand_calculation(trades, bars, number):
    actual = trades.as_dicts()[number]
    expected = EXPECTED[number]

    for field, value in expected.items():
        assert actual[field] == pytest.approx(value, abs=1e-9), field
    assert actual["entry_time"] == bars.time[expected["entry_idx"]]
    assert actual["exit_time"] == bars.time[expected["exit_idx"]]


# A bar that opens exactly on a level ---------------------------------------------------------
#
# Three bars: a signal at bar 0's close, the entry at bar 1's open, the exit on bar 2. The stop
# is 2.00 and the target 4.00 from the fill. Prices here are exact in binary floating point, so
# the open really equals the level: Spread Floor 50 points (0.50) and slippage 25 points (0.25)
# per side; every bar spread is 5 points, below the floor. Long entry: 100.00 + 0.50 + 0.25 =
# 100.75, stop 98.75, target 104.75. Short entry: 100.00 - 0.25 = 99.75, stop 101.75 and
# target 95.75, both on the ask. Bar 1 stays inside both levels on either side.
EXACT_COST = CostSnapshot(point=0.01, spread_floor_points=50, slippage_points=25)
SIGNAL_BAR = (100.0, 101.0, 99.0, 100.0)
ENTRY_BAR = (100.0, 100.5, 99.5, 100.0)
AT_OPEN = {
    # Long, bid opens on the stop 98.75: a gap exit at that open, 98.75 - 0.25 = 98.50.
    # R = (98.50 - 100.75) / 2 = -1.125. Read as a touch inside the bar, the fill is the same.
    "long opens on the stop": (1, (98.75, 99.0, 98.0, 98.5), ExitReason.STOP_GAP, 98.5, -1.125),
    # Long, bid opens on the target 104.75 and later falls through the stop: the open is
    # checked first, so the target fills: 104.75 - 0.25 = 104.50. R = 3.75 / 2 = 1.875.
    "long opens on the target": (1, (104.75, 105.0, 98.5, 99.0), ExitReason.TARGET, 104.5, 1.875),
    # Short, ask opens on the stop: bid 101.25 + 0.50 = 101.75. Exit 101.75 + 0.25 = 102.00.
    # R = (99.75 - 102.00) / 2 = -1.125. Read as a touch inside the bar, the fill is the same.
    "short opens on the stop": (-1, (101.25, 102.0, 101.0, 101.5), ExitReason.STOP_GAP, 102.0,
                                -1.125),
    # Short, ask opens on the target: bid 95.25 + 0.50 = 95.75, and the ask high 101.50 + 0.50
    # later reaches the stop. The open is checked first: exit 95.75 + 0.25 = 96.00.
    # R = (99.75 - 96.00) / 2 = 1.875.
    "short opens on the target": (-1, (95.25, 101.5, 95.0, 101.0), ExitReason.TARGET, 96.0,
                                  1.875),
}  # fmt: skip


@pytest.mark.parametrize("case", AT_OPEN)
def test_a_bar_that_opens_exactly_on_a_level_exits_at_the_open(case):
    side, exit_bar, reason, fill, r = AT_OPEN[case]
    o, h, low, c = zip(SIGNAL_BAR, ENTRY_BAR, exit_bar, strict=True)
    bars = Bars(
        symbol="GOLD",
        timeframe=Timeframe.M15,
        point=0.01,
        digits=2,
        time=[1704067200 + 900 * i for i in range(3)],
        open=o,
        high=h,
        low=low,
        close=c,
        spread=[5] * 3,
    )
    signals = Signals(
        long_entry=np.array([side == 1, False, False]),
        short_entry=np.array([side == -1, False, False]),
        stop_dist=np.full(3, 2.0),
        target_dist=np.full(3, 4.0),
    )

    [trade] = simulate(bars, signals, EXACT_COST).as_dicts()

    # Exact equality throughout: every price above is a multiple of 0.25.
    assert (trade["entry_idx"], trade["exit_idx"]) == (1, 2)
    assert trade["entry_price"] == (100.75 if side == 1 else 99.75)
    assert (trade["exit_reason"], trade["exit_price"], trade["r"]) == (reason, fill, r)
