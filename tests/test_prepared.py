"""T1: a prepared market gives the same trades as simulate, fast; the Block cache computes once.

A Trial simulates the same bars thousands of times, so `prepare` hoists the per-call setup and
`compile_signals` can share Block outputs across a grid through a BlockCache.
"""

import math
import time
from collections import Counter
from dataclasses import fields, replace
from datetime import UTC, date, datetime

import numpy as np
import pytest
import test_engine_props as props
import test_perf as perf

from vibedesk.blocks.compile import BlockCache, compile_signals
from vibedesk.blocks.registry import LIBRARY, BlockRegistry
from vibedesk.blocks.spec import expand_grid, validate
from vibedesk.engine.costs import CostSnapshot, swap_nights
from vibedesk.engine.prepared import prepare
from vibedesk.engine.simulate import Signals, simulate, simulate_prepared
from vibedesk.market.bars import Bars, Timeframe
from vibedesk.market.synthetic import SyntheticBarSource

from reference_engine import simulate_reference

BUDGET_SECONDS = 0.030


def same_records(a, b) -> bool:
    """Bit-for-bit equal, so NaN targets compare equal and nothing is rounded away."""
    return a.records.dtype == b.records.dtype and a.records.tobytes() == b.records.tobytes()


def assert_matches_reference(trades, reference):
    actual = trades.as_dicts()
    assert len(actual) == len(reference)
    for got, want in zip(actual, reference, strict=True):
        for field, value in want.items():
            if isinstance(value, float) and math.isnan(value):
                assert math.isnan(got[field]), field
            else:
                assert got[field] == pytest.approx(value, rel=1e-12, abs=1e-12), field


@pytest.mark.parametrize("seed", range(200))
def test_a_prepared_market_gives_the_same_records_as_simulate(seed):
    bars, signals, cost, m1, expected = props.run_case(**props.random_case(seed))
    market = prepare(bars, cost, m1)
    # Flipped sides give different trades on the same market; the market must not remember them.
    flipped = replace(signals, long_entry=signals.short_entry, short_entry=signals.long_entry)

    first = simulate_prepared(market, signals)
    other = simulate_prepared(market, flipped)
    again = simulate_prepared(market, signals)

    # simulate is checked against the reference twin in test_engine_props.
    assert same_records(first, expected)
    assert same_records(other, simulate(bars, flipped, cost, m1=m1))
    assert same_records(again, expected)


@pytest.mark.parametrize("seed", range(20))
def test_a_stressed_cost_prepares_the_spread_stress_market(seed):
    case = props.random_case(seed) | {"multiplier": 1.0, "use_m1": True}
    bars, signals, cost, m1, _ = props.run_case(**case)

    market = prepare(bars, cost, m1)
    stressed = prepare(bars, cost.stressed(), m1)

    np.testing.assert_array_equal(stressed.spread, 2 * market.spread)
    np.testing.assert_array_equal(stressed.m1_spread, 2 * market.m1_spread)
    np.testing.assert_array_equal(stressed.m1_lo, market.m1_lo)
    expected = simulate(bars, signals, cost.stressed(), m1=m1)
    assert same_records(simulate_prepared(stressed, signals), expected)


@pytest.mark.parametrize("triple_day", range(7))
def test_prepared_rollovers_count_the_same_swap_nights(triple_day):
    # Hourly bars over two years: every weekday, weekend and US clock change in between.
    start = int(datetime(2023, 1, 1, tzinfo=UTC).timestamp())
    n = 2 * 365 * 24
    price = np.full(n, 1.1)
    bars = Bars("EURUSD", Timeframe.H1, 0.00001, 5, start + 3600 * np.arange(n),
                price, price, price, price, np.zeros(n))  # fmt: skip
    cost = CostSnapshot(0.00001, 10, 0, triple_swap_day=triple_day)
    market = prepare(bars, cost)

    rng = np.random.default_rng(triple_day)
    entry = rng.integers(0, n, 5000)
    exit_ = np.minimum(entry + rng.integers(0, 24 * 40, 5000), n - 1)
    expected = swap_nights(bars.time[entry], bars.time[exit_], triple_day)
    np.testing.assert_array_equal(market.rollovers[exit_] - market.rollovers[entry], expected)


def test_a_signal_on_every_bar_fits_the_trade_table():
    # A stop inside the spread exits on the entry bar, so every bar holds a new trade: the
    # most trades the table can ever need.
    bars = props.synthetic_bars(3, "GOLD", Timeframe.M15)
    n = len(bars)
    cost = CostSnapshot(0.01, 40, 1)
    signals = Signals(
        long_entry=np.arange(n) % 2 == 0,
        short_entry=np.arange(n) % 2 == 1,
        stop_dist=np.full(n, 0.05),
        target_dist=np.full(n, np.nan),
    )

    trades = simulate_prepared(prepare(bars, cost), signals)

    assert len(trades) == n - 1
    assert_matches_reference(trades, simulate_reference(bars, signals, cost))


def test_no_signals_give_no_trades():
    bars = props.synthetic_bars(4, "EURUSD", Timeframe.H1)
    n = len(bars)
    never = np.zeros(n, dtype=np.bool_)
    signals = Signals(never, never, np.full(n, 0.001), np.full(n, np.nan))
    assert len(simulate_prepared(prepare(bars, CostSnapshot(0.00001, 10, 1)), signals)) == 0


@pytest.mark.perf
def test_a_prepared_simulation_over_twelve_years_of_m15_with_m1(twelve_years):
    bars, m1 = twelve_years
    [combination] = expand_grid(validate(perf.SPEC))
    cost = CostSnapshot(0.01, 55, 1, swap_long_points=-83.83, swap_short_points=20.64)
    signals = compile_signals(combination, bars)
    market = prepare(bars, cost, m1)

    trades = simulate_prepared(market, signals)  # warm numba
    timings = []
    for _ in range(5):
        start = time.perf_counter()
        simulate_prepared(market, signals)
        timings.append(time.perf_counter() - start)

    assert len(bars) > 290_000 and market.has_m1
    assert len(trades) > 1000
    assert min(timings) <= BUDGET_SECONDS, f"best of 5: {1000 * min(timings):.1f} ms"


GRID = {
    "name": "grid",
    "symbol": "GOLD",
    "timeframe": "M15",
    "direction": "both",
    "entry": {"block": "donchian_breakout", "params": {"period": [20, 40, 60, 80]}},
    "filters": [{"block": "atr_regime", "params": {"lookback": 2000, "percentile_max": 80.0}}],
    "exit": {
        "stop": {"block": "atr_stop", "params": {"mult": [1.5, 2.0, 2.5]}},
        "target": {"block": "r_target", "params": {"r": [1.5, 2.0, 3.0, 4.0]}},
    },
    "sizing": {"block": "fixed_risk", "params": {}},
}


def counting_registry(calls: Counter) -> BlockRegistry:
    """The Block Library with every Block counting its calls."""
    registry = BlockRegistry()
    for block in LIBRARY.all():

        def counted(*args, name=block.name, fn=block.fn):
            calls[name] += 1
            return fn(*args)

        registry.register(block.name, block.kind, block.params)(counted)
    return registry


def same_signals(a: Signals, b: Signals) -> bool:
    for field in fields(Signals):
        x, y = getattr(a, field.name), getattr(b, field.name)
        if isinstance(x, np.ndarray):
            if not np.array_equal(x, y, equal_nan=x.dtype.kind == "f"):
                return False
        elif x != y:
            return False
    return True


@pytest.fixture(scope="module")
def three_months():
    # About 6,000 M15 bars, so the 2000-bar percentile window fills and the filter bites.
    return SyntheticBarSource(5, date(2024, 1, 1), date(2024, 4, 1)).fetch("GOLD", Timeframe.M15)


def test_a_grid_that_shares_a_filter_computes_it_once(three_months):
    bars = three_months
    combinations = expand_grid(validate(GRID))
    calls = Counter()
    registry = counting_registry(calls)
    cache = BlockCache(bars)

    cached = [compile_signals(c, bars, registry, cache=cache) for c in combinations]

    assert len(combinations) == 48
    # Each target is computed per stop it is measured from: 4 targets x 3 stops.
    assert calls == {"atr_regime": 1, "donchian_breakout": 4, "atr_stop": 3, "r_target": 12}
    for combination, signals in zip(combinations, cached, strict=True):
        assert same_signals(signals, compile_signals(combination, bars))


def test_the_filter_bites_on_the_grid_bars(three_months):
    # Guards the test above: with an all-NaN or always-true filter, sharing it proves little.
    combination = expand_grid(validate(GRID))[0]
    unfiltered = replace(combination, filters=())
    filtered = compile_signals(combination, three_months)
    plain = compile_signals(unfiltered, three_months)
    assert 0 < filtered.long_entry.sum() < plain.long_entry.sum()


def test_without_a_cache_every_combination_computes_its_blocks():
    bars = props.synthetic_bars(6, "GOLD", Timeframe.M15)
    combinations = expand_grid(validate(GRID))[:6]
    calls = Counter()
    registry = counting_registry(calls)

    for combination in combinations:
        compile_signals(combination, bars, registry)

    assert calls["atr_regime"] == 6


def test_a_cache_belongs_to_one_set_of_bars(three_months):
    other = props.synthetic_bars(6, "GOLD", Timeframe.M15)
    combination = expand_grid(validate(GRID))[0]
    cache = BlockCache(three_months)
    compile_signals(combination, three_months, cache=cache)

    with pytest.raises(ValueError, match="other bars"):
        compile_signals(combination, other, cache=cache)


def test_shared_block_outputs_are_read_only(three_months):
    cache = BlockCache(three_months)
    a, b = (compile_signals(c, three_months, cache=cache) for c in expand_grid(validate(GRID))[:2])
    assert a.stop_dist is b.stop_dist  # shared, so nobody may write to it
    with pytest.raises(ValueError, match="read-only"):
        a.stop_dist[0] = 1.0


# Signals confined to a window cost a window of bars ---------------------------------------------


@pytest.fixture(scope="module")
def two_years_h1():
    source = SyntheticBarSource(5, date(2022, 1, 3), date(2024, 1, 1))
    return source.fetch("GOLD", Timeframe.H1), source.fetch("GOLD", Timeframe.M1)


@pytest.mark.parametrize("month", [1, 6, 12])
def test_signals_in_one_month_give_the_reference_trades(two_years_h1, month):
    # The engine skips the bars before the first signal and stops once flat after the last.
    # A trade opened by the month's last signal must still run to its real exit, not be cut.
    bars, m1 = two_years_h1
    spec = validate(perf.SPEC | {"timeframe": "H1"})
    [combination] = expand_grid(spec)
    signals = compile_signals(combination, bars)
    month_start = int(datetime(2022, month, 1, tzinfo=UTC).timestamp())
    inside = (bars.time >= month_start) & (bars.time < month_start + 31 * 86400)
    confined = replace(
        signals,
        long_entry=signals.long_entry & inside,
        short_entry=signals.short_entry & inside,
    )
    cost = CostSnapshot(0.01, 55, 1, swap_long_points=-83.83, swap_short_points=20.64)

    trades = simulate_prepared(prepare(bars, cost, m1), confined)

    assert len(trades) > 0
    assert_matches_reference(trades, simulate_reference(bars, confined, cost, m1=m1))


@pytest.mark.perf
def test_a_month_of_signals_on_twelve_years_costs_far_less_than_twelve_years(twelve_years):
    bars, m1 = twelve_years
    [combination] = expand_grid(validate(perf.SPEC))
    signals = compile_signals(combination, bars)
    market = prepare(bars, CostSnapshot(0.01, 55, 1), m1)
    month = (bars.time >= bars.time[0] + 86400 * 365 * 6) & (
        bars.time < bars.time[0] + 86400 * (365 * 6 + 31)
    )
    confined = replace(
        signals, long_entry=signals.long_entry & month, short_entry=signals.short_entry & month
    )

    def best(sig):
        simulate_prepared(market, sig)
        timings = []
        for _ in range(5):
            start = time.perf_counter()
            simulate_prepared(market, sig)
            timings.append(time.perf_counter() - start)
        return min(timings)

    assert best(confined) < best(signals) / 4
