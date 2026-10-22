"""Spec §9.5: no Block may look ahead. Its output at bar t must not change when bars after t do.

Every registered Block except sizing must have parameter sets in PARAMS, so a new Block can't
be added without this test covering it.
"""

from datetime import date
from functools import cache

import numpy as np
import pytest

from vibedesk.blocks.exits import AtrStopParams, atr_stop
from vibedesk.blocks.registry import LIBRARY, Kind
from vibedesk.market.bars import Bars, Timeframe
from vibedesk.market.synthetic import SyntheticBarSource

PARAMS = {
    "donchian_breakout": [{"period": 5}, {"period": 40}],
    "ma_cross": [{"fast": 3, "slow": 8}, {"fast": 10, "slow": 50, "type": "ema"}],
    "rsi_threshold": [
        {"period": 5, "level": 35},
        {"period": 14, "level": 25, "mode": "cross_down"},
    ],
    "bollinger_touch": [
        {"period": 10, "stdev": 1.5},
        {"period": 30, "stdev": 2.5, "mode": "break"},
    ],
    "pullback_to_ma": [
        {"trend_period": 50, "pullback_period": 10},
        {"trend_period": 30, "pullback_period": 5, "depth_atr": 0.5},
    ],
    "range_breakout": [
        {"session": "london_open", "range_bars": 4},
        {"session": "ny_open", "range_bars": 2},
        {"session": "asia", "range_bars": 8},
    ],
    "atr_stop": [{"period": 5, "mult": 1.0}, {"period": 30, "mult": 2.5}],
    "r_target": [{"r": 1.5}],
    "ma_slope": [{"period": 20}, {"period": 50, "min_slope_atr": 0.3}],
    "adx": [{"period": 7, "min": 20}, {"period": 14, "max": 30}],
    "atr_regime": [
        {"period": 5, "lookback": 40, "percentile_min": 60},
        {"period": 14, "lookback": 100, "percentile_max": 40},
    ],
    "session_window": [
        {"zone": "london", "start": "08:00", "end": "12:00"},
        {"zone": "tokyo", "start": "22:00", "end": "02:00"},
    ],
    "day_of_week": [{"mon": False}, {"wed": False, "fri": False}],
}

CHECKED_KINDS = {Kind.ENTRY, Kind.FILTER, Kind.STOP, Kind.TARGET}
CASES_PER_BLOCK = 100


def test_every_block_has_look_ahead_parameters():
    registered = {b.name for b in LIBRARY.all() if b.kind in CHECKED_KINDS}

    assert registered == set(PARAMS)


@cache
def base_bars(seed: int) -> Bars:
    return SyntheticBarSource(seed, date(2024, 3, 4), date(2024, 3, 25)).fetch(
        "GOLD", Timeframe.M15
    )


def rewrite_after(bars: Bars, cut: int, rng: np.random.Generator) -> Bars:
    """The same bars up to `cut`, then a different, still valid, random future."""
    n = len(bars)
    future = n - cut - 1
    step = bars.close[cut] * 0.002
    close = bars.close[cut] + np.cumsum(rng.normal(0, step, future))
    open_ = np.r_[bars.close[cut], close[:-1]]
    high = np.maximum(open_, close) + rng.uniform(0, step, future)
    low = np.minimum(open_, close) - rng.uniform(0, step, future)

    def splice(head, tail):
        return np.r_[head[: cut + 1], tail]

    return Bars(
        bars.symbol,
        bars.timeframe,
        bars.point,
        bars.digits,
        bars.time,
        splice(bars.open, open_),
        splice(bars.high, high),
        splice(bars.low, low),
        splice(bars.close, close),
        splice(bars.spread, rng.integers(0, 200, future)),
    )


def outputs(block, params, bars) -> list[np.ndarray]:
    if block.kind is Kind.TARGET:
        stop_dist = atr_stop(bars, AtrStopParams())
        result = block.fn(bars, params, stop_dist)
    else:
        result = block.fn(bars, params)
    return [np.asarray(x) for x in (result if isinstance(result, tuple) else (result,))]


@pytest.mark.parametrize("name", sorted(PARAMS))
def test_block_never_looks_ahead(name):
    block = LIBRARY.get(name)
    rng = np.random.default_rng(abs(hash(name)) % 2**32)
    signalled = False

    for case in range(CASES_PER_BLOCK):
        params = block.params(**PARAMS[name][case % len(PARAMS[name])])
        bars = base_bars(case % 5)
        cut = int(rng.integers(50, len(bars) - 2))
        original = outputs(block, params, bars)
        changed = outputs(block, params, rewrite_after(bars, cut, rng))

        for before, after in zip(original, changed, strict=True):
            assert np.array_equal(before[: cut + 1], after[: cut + 1], equal_nan=True), (
                f"{name}{PARAMS[name][case % len(PARAMS[name])]} changed at or before bar {cut}"
            )
        signalled = signalled or any(np.any(x[: cut + 1] != 0) for x in original if x.dtype == bool)
        signalled = signalled or any(np.isfinite(x).any() for x in original if x.dtype != bool)

    assert signalled, f"{name} never produced output, so the test proved nothing"
