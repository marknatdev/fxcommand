"""T4: the full Cost Model. Swap nights, M1 settling of same-bar conflicts, stress, snapshots."""

from datetime import UTC, datetime

import numpy as np
import pytest

from vibedesk.blocks.compile import compile_signals
from vibedesk.blocks.spec import expand_grid, validate
from vibedesk.engine.costs import CostSnapshot, swap_nights
from vibedesk.engine.simulate import ExitReason, simulate
from vibedesk.market.bars import Bars, Timeframe

WINTER = 2 * 3600  # January: server clock = UTC + 2 h


def server(*args) -> int:
    """UTC epoch seconds of a January 2026 server-clock time."""
    return int(datetime(*args, tzinfo=UTC).timestamp()) - WINTER


def donchian_spec(timeframe="M15", r=2.0):
    return validate(
        {
            "name": "costs",
            "symbol": "GOLD",
            "timeframe": timeframe,
            "direction": "both",
            "entry": {"block": "donchian_breakout", "params": {"period": 3}},
            "exit": {
                "stop": {"block": "atr_stop", "params": {"period": 3, "mult": 1.0}},
                "target": {"block": "r_target", "params": {"r": r}},
            },
            "sizing": {"block": "fixed_risk", "params": {}},
        }
    )


def gold_bars(timeframe, times, ohlc, spread=5):
    o, h, low, c = zip(*ohlc, strict=True) if ohlc else ((), (), (), ())
    return Bars("GOLD", timeframe, 0.01, 2, times, o, h, low, c, [spread] * len(times))


# Swap nights -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry, exit_, nights",
    [
        (server(2026, 1, 13, 10), server(2026, 1, 16, 10), 5),  # Tue -> Fri: 1 + 3 (Wed) + 1
        (server(2026, 1, 16, 10), server(2026, 1, 19, 10), 1),  # Fri -> Mon: the weekend is one
        (server(2026, 1, 12, 10), server(2026, 1, 12, 22), 0),  # same server day
        (server(2026, 1, 12, 10), server(2026, 1, 19, 10), 7),  # one whole week
        (server(2026, 1, 13, 0), server(2026, 1, 14, 0), 1),  # opened at a rollover: not charged
    ],
)
def test_swap_nights_follow_the_server_rollover(entry, exit_, nights):
    assert swap_nights(np.array([entry]), np.array([exit_]), triple_day=3)[0] == nights


def test_a_three_night_hold_over_wednesday_pays_five_nights_of_swap():
    # D1 bars open at server midnight. Flat Wed-Fri, a breakout on Monday, entry on Tuesday,
    # and the target is hit on Friday: rollovers into Wed (1), Thu (3, Wednesday's) and Fri (1).
    days = [7, 8, 9, 12, 13, 14, 15, 16]  # January 2026
    flat = (100.0, 101.0, 99.0, 100.0)
    ohlc = [
        flat,
        flat,
        flat,
        (100.0, 102.0, 100.0, 101.5),  # Mon: long signal
        (101.6, 103.0, 101.0, 102.8),  # Tue: enter 101.71, stop 99.71, target 105.71
        (102.9, 104.5, 102.5, 104.3),  # Wed
        (104.4, 105.5, 104.0, 105.0),  # Thu
        (105.1, 106.5, 104.5, 106.0),  # Fri: target
    ]
    bars = gold_bars(Timeframe.D1, [server(2026, 1, d) for d in days], ohlc)
    cost = CostSnapshot(0.01, 10, 1, swap_long_points=-83.83, swap_short_points=20.64)
    [combination] = expand_grid(donchian_spec("D1"))

    [trade] = simulate(bars, compile_signals(combination, bars), cost).as_dicts()

    assert trade["exit_reason"] is ExitReason.TARGET
    swap_r = -83.83 * 5 * 0.01 / 2.0
    assert trade["swap_r"] == pytest.approx(swap_r, abs=1e-9)
    assert trade["r"] == pytest.approx(1.995 + swap_r, abs=1e-9)  # the swap ate the whole win


# M1 settles same-bar conflicts -----------------------------------------------------------------

FLAT = (100.0, 101.0, 99.0, 100.0)
M15_OHLC = [FLAT, FLAT, FLAT, (100.0, 102.0, 100.0, 101.5), (101.6, 106.0, 99.5, 102.0)]
# Entry at bar 4's open: 101.71, stop 99.71, target 105.71. Bar 4 touches both.
TARGET_FIRST = [
    (101.6, 103.0, 101.5, 102.9),
    (102.9, 106.0, 102.8, 105.9),
    (105.9, 105.9, 99.5, 102.0),
]
STOP_FIRST = [
    (101.6, 102.0, 99.5, 100.0),
    (100.0, 106.0, 99.9, 105.9),
    (105.9, 105.9, 102.0, 102.0),
]


def conflict_case(minutes):
    times = [server(2026, 1, 12, 10) + 900 * i for i in range(5)]
    bars = gold_bars(Timeframe.M15, times, M15_OHLC)
    m1 = None
    if minutes is not None:
        m1 = gold_bars(Timeframe.M1, [times[4] + 60 * i for i in range(len(minutes))], minutes)
    [combination] = expand_grid(donchian_spec())
    cost = CostSnapshot(0.01, 10, 1)
    [trade] = simulate(bars, compile_signals(combination, bars), cost, m1=m1).as_dicts()
    return trade


def test_m1_shows_the_target_came_first():
    trade = conflict_case(TARGET_FIRST)

    assert trade["exit_reason"] is ExitReason.TARGET
    assert trade["exit_price"] == pytest.approx(105.70, abs=1e-9)


def test_m1_shows_the_stop_came_first():
    trade = conflict_case(STOP_FIRST)

    assert trade["exit_reason"] is ExitReason.STOP
    assert trade["exit_price"] == pytest.approx(99.70, abs=1e-9)


@pytest.mark.parametrize("minutes", [None, []], ids=["no M1 given", "no M1 inside the bar"])
def test_without_m1_the_stop_wins(minutes):
    assert conflict_case(minutes)["exit_reason"] is ExitReason.STOP


def test_m1_outside_the_conflict_bar_is_ignored():
    early = [(100.0, 106.0, 99.9, 105.0)]  # a minute that hits the target, but before entry
    times = [server(2026, 1, 12, 10) + 900 * i for i in range(5)]
    bars = gold_bars(Timeframe.M15, times, M15_OHLC)
    m1 = gold_bars(Timeframe.M1, [times[3]], early)
    [combination] = expand_grid(donchian_spec())

    [trade] = simulate(
        bars, compile_signals(combination, bars), CostSnapshot(0.01, 10, 1), m1=m1
    ).as_dicts()

    assert trade["exit_reason"] is ExitReason.STOP


# Stress and snapshots --------------------------------------------------------------------------


def test_stress_doubles_every_spread():
    cost = CostSnapshot(0.01, 55, 1)
    bar_spreads = np.array([10, 55, 80])

    assert cost.spread_prices(bar_spreads).tolist() == pytest.approx([0.55, 0.55, 0.80])
    assert cost.stressed().spread_prices(bar_spreads).tolist() == pytest.approx([1.10, 1.10, 1.60])


def test_cost_snapshot_round_trips_through_json():
    cost = CostSnapshot(
        point=0.01,
        spread_floor_points=55,
        slippage_points=1,
        swap_long_points=-83.83,
        swap_short_points=20.64,
        triple_swap_day=3,
        spread_multiplier=2.0,
        as_of="2026-10-21",
    )

    assert CostSnapshot.from_json(cost.to_json()) == cost


# Shorts settle on the ask, using each minute's own spread -------------------------------------

SHORT_M15 = [FLAT, FLAT, FLAT, (100.0, 100.0, 98.0, 98.5), (98.4, 100.5, 94.0, 98.0)]
# Short at bar 4's bid open: 98.4 - 0.01 = 98.39; stop 100.39; target 94.39 (both on the ask).


def short_conflict_case(minutes, minute_spreads):
    times = [server(2026, 1, 12, 10) + 900 * i for i in range(5)]
    bars = gold_bars(Timeframe.M15, times, SHORT_M15)
    o, h, low, c = zip(*minutes, strict=True)
    m1_times = [times[4] + 60 * i for i in range(len(minutes))]
    m1 = Bars("GOLD", Timeframe.M1, 0.01, 2, m1_times, o, h, low, c, minute_spreads)
    [combination] = expand_grid(donchian_spec())
    cost = CostSnapshot(0.01, 10, 1)
    [trade] = simulate(bars, compile_signals(combination, bars), cost, m1=m1).as_dicts()
    assert trade["side"] == -1 and trade["entry_idx"] == 4
    return trade


def test_short_m1_shows_the_target_came_first():
    minutes = [
        (98.4, 98.6, 96.0, 96.2),
        (96.2, 96.3, 94.0, 94.1),  # ask low 94.10 <= 94.39
        (94.1, 100.5, 94.1, 98.0),
    ]

    trade = short_conflict_case(minutes, [5, 5, 5])

    assert trade["exit_reason"] is ExitReason.TARGET
    assert trade["exit_price"] == pytest.approx(94.40, abs=1e-9)  # bought back at target + slip


def test_short_m1_stop_triggers_on_the_minute_own_wider_spread():
    # Minute 1's bid high is 100.25: with the 10-point floor the ask is 100.35, short of the
    # stop. But that minute's own spread is 30 points, so its ask reaches 100.55: stopped.
    minutes = [
        (98.4, 98.6, 98.3, 98.5),
        (98.5, 100.25, 98.5, 100.0),
        (100.0, 100.0, 94.0, 94.5),  # the target only comes afterwards
    ]

    trade = short_conflict_case(minutes, [5, 30, 5])

    assert trade["exit_reason"] is ExitReason.STOP
    assert trade["exit_price"] == pytest.approx(100.40, abs=1e-9)
    assert trade["cost_r"] == pytest.approx((0.01 + 0.30 + 0.01) / 2.0, abs=1e-9)
