"""T7: hand-checked atr_target, atr_trailing, time_exit and opposite_signal exits.

Same conventions as the main golden series: GOLD M15 bid prices, point 0.01, Spread Floor
10 points (0.10), slippage 0.01 per side. Every bar's range is 2.00 and contains the previous
close, so ATR(3) is exactly 2.00. Donchian(3) fires long at bar 3; the trade enters at bar 4's
open, 101.6 + 0.10 + 0.01 = 101.71.
"""

import pytest

from vibedesk.blocks.compile import compile_signals
from vibedesk.blocks.spec import expand_grid, validate
from vibedesk.engine.costs import CostSnapshot
from vibedesk.engine.simulate import ExitReason, simulate
from vibedesk.market.bars import Bars, Timeframe

FLAT = (100.0, 101.0, 99.0, 100.0)
BREAKOUT = (100.0, 102.0, 100.0, 101.5)  # bar 3: long signal
RISE = [
    (101.6, 103.0, 101.0, 102.8),  # bar 4: entry
    (102.9, 104.5, 102.5, 104.3),  # 5
    (104.4, 106.0, 104.0, 105.0),  # 6
]
PULLBACK = (105.0, 105.5, 103.5, 103.8)  # 7: falls, but stays above the trailing stop
BREAKDOWN = (103.8, 104.5, 102.5, 103.0)  # 8: low reaches the trailing stop


def run(ohlc, exit_spec, direction="both", filters=()):
    o, h, low, c = zip(*ohlc, strict=True)
    n = len(ohlc)
    bars = Bars("GOLD", Timeframe.M15, 0.01, 2, [1704067200 + 900 * i for i in range(n)],
                o, h, low, c, [5] * n)  # fmt: skip
    spec = validate(
        {
            "name": "exits",
            "symbol": "GOLD",
            "timeframe": "M15",
            "direction": direction,
            "entry": {"block": "donchian_breakout", "params": {"period": 3}},
            "filters": list(filters),
            "exit": exit_spec,
            "sizing": {"block": "fixed_risk", "params": {}},
        }
    )
    [combination] = expand_grid(spec)
    cost = CostSnapshot(point=0.01, spread_floor_points=10, slippage_points=1)
    return simulate(bars, compile_signals(combination, bars), cost).as_dicts()


def stop(mult):
    return {"block": "atr_stop", "params": {"period": 3, "mult": mult}}


def mirror(ohlc):
    """Reflect bars around 100, swapping highs and lows, to turn a long case into a short."""
    return [(200 - o, 200 - lo, 200 - h, 200 - c) for o, h, lo, c in ohlc]


def test_atr_target():
    # Target 1.5 * ATR = 3.00 from the fill: 104.71. Bar 6's high of 106 reaches it.
    [trade] = run(
        [FLAT, FLAT, FLAT, BREAKOUT, *RISE],
        {
            "stop": stop(1.0),
            "target": {"block": "atr_target", "params": {"period": 3, "mult": 1.5}},
        },
    )

    assert trade["exit_reason"] is ExitReason.TARGET
    assert trade["exit_idx"] == 6
    assert trade["exit_price"] == pytest.approx(104.70, abs=1e-9)
    assert trade["r"] == pytest.approx(2.99 / 2, abs=1e-9)


TRAILING = {
    "stop": stop(2.0),
    "trailing": {"block": "atr_trailing", "params": {"period": 3, "mult": 1.0}},
}


def test_atr_trailing_ratchets_on_closes_and_never_loosens():
    # Stop 2 ATR away: 97.71. The trail is close - 2.00 after each close:
    # bar 4 100.8, bar 5 102.3, bar 6 103.0; bar 7 would give 101.8, but the trail stays at 103.0.
    # Bar 8's low of 102.5 reaches 103.0: exit 102.99.
    [trade] = run([FLAT, FLAT, FLAT, BREAKOUT, *RISE, PULLBACK, BREAKDOWN], TRAILING)

    assert trade["exit_reason"] is ExitReason.TRAIL
    assert trade["exit_idx"] == 8
    assert trade["exit_price"] == pytest.approx(102.99, abs=1e-9)
    assert trade["stop_price"] == pytest.approx(97.71, abs=1e-9)  # the initial stop is recorded
    assert trade["r"] == pytest.approx(1.28 / 4, abs=1e-9)


def test_atr_trailing_for_a_short_trails_the_ask_close():
    # The mirror image: short at 98.39, stop 102.39. The trail is the ask close + 2.00:
    # bar 4 99.3, bar 5 97.8, bar 6 97.1; bar 7 would give 98.3 but stays at 97.1. Bar 8's ask
    # high, 97.5 + 0.10, reaches it: buy back at 97.11.
    [trade] = run(mirror([FLAT, FLAT, FLAT, BREAKOUT, *RISE, PULLBACK, BREAKDOWN]), TRAILING)

    assert trade["side"] == -1
    assert trade["exit_reason"] is ExitReason.TRAIL
    assert trade["exit_price"] == pytest.approx(97.11, abs=1e-9)
    assert trade["r"] == pytest.approx(1.28 / 4, abs=1e-9)
    assert trade["cost_r"] == pytest.approx(0.12 / 4, abs=1e-9)


def test_time_exit_leaves_at_the_open_after_n_bars():
    # Entered at bar 4; two bars later, at bar 6's open of 104.4, it leaves: 104.39.
    [trade] = run(
        [FLAT, FLAT, FLAT, BREAKOUT, *RISE],
        {"stop": stop(1.0), "time": {"block": "time_exit", "params": {"bars": 2}}},
    )

    assert trade["exit_reason"] is ExitReason.TIME
    assert trade["exit_idx"] == 6
    assert trade["exit_price"] == pytest.approx(104.39, abs=1e-9)
    assert trade["r"] == pytest.approx(2.68 / 2, abs=1e-9)


def test_opposite_signal_closes_a_long_only_trade_without_reversing():
    # Long-only, stop 2 ATR away (97.71). Bar 6 closes at 99.6, below the lowest low of bars 3-5
    # (100): Donchian's short signal. The long leaves at bar 7's open, 99.5 - 0.01 = 99.49, and
    # no short is opened, because the Spec is long-only.
    bars = [
        FLAT, FLAT, FLAT, BREAKOUT,
        (101.6, 103.0, 101.0, 102.8),  # 4: entry
        (102.8, 103.0, 101.0, 101.2),  # 5
        (101.2, 101.5, 99.5, 99.6),  # 6: short signal
        (99.5, 100.5, 98.5, 100.0),  # 7: exit at the open
    ]  # fmt: skip
    trades = run(
        bars, {"stop": stop(2.0), "opposite": {"block": "opposite_signal"}}, direction="long"
    )

    [trade] = trades
    assert trade["exit_reason"] is ExitReason.OPPOSITE
    assert trade["exit_idx"] == 7
    assert trade["exit_price"] == pytest.approx(99.49, abs=1e-9)
    assert trade["r"] == pytest.approx(-2.22 / 4, abs=1e-9)


REVERSAL_BARS = [
    FLAT, FLAT, FLAT, BREAKOUT,
    (101.6, 103.0, 101.0, 102.8),  # 4: long entry
    (102.8, 103.0, 101.0, 101.2),  # 5
    (101.2, 101.5, 99.5, 99.6),  # 6: short signal, ATR(3) = 2.00
    (99.5, 100.5, 98.5, 100.0),  # 7: the long leaves and the short enters at this open
]  # fmt: skip
OPPOSITE = {"stop": stop(2.0), "opposite": {"block": "opposite_signal"}}


def test_opposite_signal_reverses_a_two_way_trade_at_the_same_open():
    # The long leaves at bar 7's open, 99.5 - 0.01 = 99.49, exactly as when it only closes.
    # The short enters at that same open: sold at the bid, 99.49, with a stop 2 x ATR(3) = 4.00
    # away on the ask, 103.49. It is still open at the last bar and closes at its ask close,
    # 100.0 + 0.10 + 0.01 = 100.11: R = -(100.11 - 99.49) / 4.
    long, short = run(REVERSAL_BARS, OPPOSITE)

    assert (long["exit_reason"], long["exit_idx"]) == (ExitReason.OPPOSITE, 7)
    assert long["exit_price"] == pytest.approx(99.49, abs=1e-9)
    assert (short["side"], short["entry_idx"]) == (-1, 7)
    assert short["entry_price"] == pytest.approx(99.49, abs=1e-9)
    assert short["stop_price"] == pytest.approx(103.49, abs=1e-9)
    assert short["exit_reason"] is ExitReason.END
    assert short["exit_price"] == pytest.approx(100.11, abs=1e-9)
    assert short["r"] == pytest.approx(-0.62 / 4, abs=1e-9)


def test_a_filtered_reverse_signal_only_closes():
    # Bars open at 00:00 UTC (London midnight, GMT). The window judges where each trade would
    # open: London 01:00-01:45 lets the long through (signal 00:45, entry 01:00) but not the
    # short (signal 01:30, entry 01:45). The opposite signal comes from the entry Block alone
    # and still closes the long, but the filter blocks the reversal.
    window = {
        "block": "session_window",
        "params": {"zone": "london", "start": "01:00", "end": "01:45"},
    }

    [trade] = run(REVERSAL_BARS, OPPOSITE, filters=[window])

    assert (trade["side"], trade["exit_reason"], trade["exit_idx"]) == (1, ExitReason.OPPOSITE, 7)


TRAIL_AND_TIME = TRAILING | {"time": {"block": "time_exit", "params": {"bars": 4}}}


def test_trailing_stop_beats_a_time_exit_due_at_the_same_open():
    # The time exit is due at bar 8's open, but that open (102.9) is already below the trail
    # (103.0). The trailing stop comes first in priority: TRAIL, at the open.
    gap_down = (102.9, 103.5, 102.0, 103.0)
    [trade] = run([FLAT, FLAT, FLAT, BREAKOUT, *RISE, PULLBACK, gap_down], TRAIL_AND_TIME)

    assert trade["exit_reason"] is ExitReason.TRAIL
    assert trade["exit_price"] == pytest.approx(102.89, abs=1e-9)


def test_time_exit_at_the_open_beats_a_trailing_stop_hit_later_in_the_bar():
    # Bar 8 opens above the trail (103.8 > 103.0), so the time exit due at the open goes first,
    # even though the bar's low later reaches the trail.
    [trade] = run([FLAT, FLAT, FLAT, BREAKOUT, *RISE, PULLBACK, BREAKDOWN], TRAIL_AND_TIME)

    assert trade["exit_reason"] is ExitReason.TIME
    assert trade["exit_price"] == pytest.approx(103.79, abs=1e-9)
