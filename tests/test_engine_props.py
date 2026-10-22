"""The engine against its independent reference twin, plus invariants that must always hold."""

import math
from datetime import date
from functools import cache

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vibedesk.blocks.compile import compile_signals
from vibedesk.blocks.spec import expand_grid, validate
from vibedesk.engine.costs import CostSnapshot
from vibedesk.engine.simulate import ExitReason, simulate
from vibedesk.market.bars import Timeframe
from vibedesk.market.synthetic import PROFILES, SyntheticBarSource

from reference_engine import simulate_reference

START, END = date(2024, 3, 4), date(2024, 3, 18)


@cache
def synthetic_bars(seed: int, symbol: str, timeframe: Timeframe):
    return SyntheticBarSource(seed, START, END).fetch(symbol, timeframe)


def run_case(
    seed,
    symbol,
    timeframe,
    period,
    atr_period,
    mult,
    r,
    direction,
    floor,
    slip,
    swap_long=0.0,
    swap_short=0.0,
    multiplier=1.0,
    use_m1=False,
):
    bars = synthetic_bars(seed, symbol, timeframe)
    m1 = synthetic_bars(seed, symbol, Timeframe.M1) if use_m1 else None
    target = {"target": {"block": "r_target", "params": {"r": r}}} if r else {}
    spec = validate(
        {
            "name": "props",
            "symbol": symbol,
            "timeframe": timeframe.value,
            "direction": direction,
            "entry": {"block": "donchian_breakout", "params": {"period": period}},
            "exit": {"stop": {"block": "atr_stop", "params": {"period": atr_period, "mult": mult}}}
            | target,
            "sizing": {"block": "fixed_risk", "params": {}},
        }
    )
    [combination] = expand_grid(spec)
    signals = compile_signals(combination, bars)
    cost = CostSnapshot(
        PROFILES[symbol].point,
        floor,
        slip,
        swap_long_points=swap_long,
        swap_short_points=swap_short,
        spread_multiplier=multiplier,
    )
    return bars, signals, cost, m1, simulate(bars, signals, cost, m1=m1)


def random_case(seed: int):
    rng = np.random.default_rng(seed)
    return dict(
        seed=seed,
        symbol=str(rng.choice(["GOLD", "EURUSD", "USDJPY"])),
        timeframe=Timeframe(rng.choice(["M15", "M15", "M15", "H1", "H4"])),
        period=int(rng.integers(3, 40)),
        atr_period=int(rng.integers(3, 30)),
        mult=float(rng.choice([0.5, 1.0, 1.5, 2.0, 3.0])),
        r=float(rng.choice([0.0, 1.0, 2.0, 3.0])),  # 0.0 means no target
        direction=str(rng.choice(["long", "short", "both"])),
        floor=float(rng.integers(0, 60)),
        slip=float(rng.integers(0, 4)),
        swap_long=float(rng.uniform(-90, 25)),
        swap_short=float(rng.uniform(-90, 25)),
        multiplier=float(rng.choice([1.0, 1.0, 2.0])),
        use_m1=bool(rng.random() < 0.7),
    )


@pytest.mark.parametrize("seed", range(200))
def test_engine_matches_the_reference_twin(seed):
    bars, signals, cost, m1, trades = run_case(**random_case(seed))
    expected = simulate_reference(bars, signals, cost, m1=m1)

    actual = trades.as_dicts()
    assert len(actual) == len(expected)
    for got, want in zip(actual, expected, strict=True):
        for field, value in want.items():
            if isinstance(value, float) and math.isnan(value):
                assert math.isnan(got[field]), field
            else:
                assert got[field] == pytest.approx(value, rel=1e-12, abs=1e-12), field


case_strategy = st.builds(
    dict,
    seed=st.integers(0, 10_000),
    symbol=st.sampled_from(["GOLD", "EURUSD"]),
    timeframe=st.just(Timeframe.M15),
    period=st.integers(2, 60),
    atr_period=st.integers(2, 40),
    mult=st.floats(0.3, 5.0),
    r=st.sampled_from([0.0, 0.5, 1.0, 2.0, 4.0]),
    direction=st.sampled_from(["long", "short", "both"]),
    floor=st.floats(0, 80),
    slip=st.floats(0, 5),
)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(case=case_strategy)
def test_trade_invariants(case):
    bars, _, cost, _, trades = run_case(**case)
    slip = cost.slippage_price
    t = trades.records
    reason = t["exit_reason"]
    slip_r = slip / t["risk"]
    eps = 1e-9

    # Positions never overlap; a new entry comes at least one bar after the last exit.
    assert np.all(t["entry_idx"][1:] > t["exit_idx"][:-1])
    assert np.all(t["exit_idx"] >= t["entry_idx"])
    assert np.all(t["risk"] > 0)

    # Swap is reported on its own; the price part of R obeys the fill rules below.
    assert np.all(t["swap_r"] == 0)

    # A plain stop costs exactly 1R plus the exit slippage; a gap through the stop costs more.
    stop = reason == ExitReason.STOP
    assert np.allclose(t["r"][stop], -1 - slip_r[stop])
    gap = reason == ExitReason.STOP_GAP
    assert np.all(t["r"][gap] <= -1 - slip_r[gap] + eps)

    # Nothing but a gap loses more than 1R plus slippage.
    assert np.all(t["r"][~gap] >= -1 - slip_r[~gap] - eps)

    # A stop already breached at the entry open (inside the spread) is a gap exit at the market,
    # never a fill at the stop price.
    entry_spread = cost.spread_prices(bars.spread)[t["entry_idx"]]
    entry_open = bars.open[t["entry_idx"]]
    breached = np.where(
        t["side"] == 1, entry_open <= t["stop_price"], entry_open + entry_spread >= t["stop_price"]
    )
    assert np.all(reason[breached] == ExitReason.STOP_GAP)
    assert np.all(t["exit_idx"][breached] == t["entry_idx"][breached])
    same_bar_stop = stop & (t["exit_idx"] == t["entry_idx"])
    assert not np.any(same_bar_stop & breached)

    # No trade earns more than its target; a target exit earns exactly the target minus slippage.
    has_target = ~np.isnan(t["target_price"])
    target_r = np.abs(t["target_price"] - t["entry_price"]) / t["risk"]
    assert np.all(t["r"][has_target] <= target_r[has_target] + eps)
    hit = reason == ExitReason.TARGET
    assert np.allclose(t["r"][hit], target_r[hit] - slip_r[hit])
    assert not np.any(hit & ~has_target)

    # Costs are never negative, and spread is charged at least once per trade.
    assert np.all(t["cost_r"] >= cost.spread_floor_points * cost.point / t["risk"] - eps)

    if case["direction"] == "long":
        assert np.all(t["side"] == 1)
    if case["direction"] == "short":
        assert np.all(t["side"] == -1)


@pytest.mark.parametrize("seed", range(40))
def test_stress_never_improves_a_trade_that_exits_the_same_way(seed):
    case = random_case(seed) | {"multiplier": 1.0}
    *_, base = run_case(**case)
    *_, stressed = run_case(**case | {"multiplier": 2.0})

    def key(t):
        return list(zip(t["entry_idx"], t["exit_idx"], t["exit_reason"], strict=True))

    base_r = dict(zip(key(base.records), base["r"], strict=True))
    for k, r in zip(key(stressed.records), stressed["r"], strict=True):
        if k in base_r:
            assert r <= base_r[k] + 1e-9


def test_stress_lowers_total_r_across_many_cases():
    base_total = stressed_total = 0.0
    for seed in range(60):
        case = random_case(seed) | {"multiplier": 1.0}
        base_total += run_case(**case)[-1]["r"].sum()
        stressed_total += run_case(**case | {"multiplier": 2.0})[-1]["r"].sum()

    assert stressed_total < base_total
