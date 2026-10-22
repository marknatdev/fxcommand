"""Spec §10, M1: one combination over 12 years of M15 takes at most 0.5 s once numba is warm.

Timed: compiling signals, simulating with M1 settling, and the metrics. The synthetic data
comes from the shared `twelve_years` fixture and is not timed. The best of three runs is used,
so a busy machine doesn't fail the test.
"""

import time

import pytest

from vibedesk.blocks.compile import compile_signals
from vibedesk.blocks.spec import expand_grid, validate
from vibedesk.engine.costs import CostSnapshot
from vibedesk.engine.metrics import metrics
from vibedesk.engine.simulate import simulate

BUDGET_SECONDS = 0.5

SPEC = {
    "name": "benchmark",
    "symbol": "GOLD",
    "timeframe": "M15",
    "direction": "both",
    "entry": {"block": "donchian_breakout", "params": {"period": 20}},
    "filters": [{"block": "session_window", "params": {"zone": "london"}}],
    "exit": {
        "stop": {"block": "atr_stop", "params": {"period": 14, "mult": 2.0}},
        "target": {"block": "r_target", "params": {"r": 2.0}},
        "trailing": {"block": "atr_trailing", "params": {"period": 14, "mult": 3.0}},
        "time": {"block": "time_exit", "params": {"bars": 96}},
    },
    "sizing": {"block": "fixed_risk", "params": {}},
}


@pytest.mark.perf
def test_one_combination_over_twelve_years_of_m15(twelve_years):
    bars, m1 = twelve_years
    [combination] = expand_grid(validate(SPEC))
    cost = CostSnapshot(0.01, 55, 1, swap_long_points=-83.83, swap_short_points=20.64)

    def one_combination():
        trades = simulate(bars, compile_signals(combination, bars), cost, m1=m1)
        return trades, metrics(trades, bars)

    one_combination()  # warm numba
    timings = []
    for _ in range(3):
        start = time.perf_counter()
        trades, result = one_combination()
        timings.append(time.perf_counter() - start)

    assert len(bars) > 290_000  # about 12 years of M15
    assert result.trades > 1000
    assert min(timings) <= BUDGET_SECONDS, f"best of 3: {min(timings):.3f} s"
