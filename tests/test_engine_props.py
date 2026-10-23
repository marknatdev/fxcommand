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
    atr_target=0.0,
    trail=0.0,
    time_bars=0,
    opposite=False,
):
    bars = synthetic_bars(seed, symbol, timeframe)
    m1 = synthetic_bars(seed, symbol, Timeframe.M1) if use_m1 else None
    exits = {"stop": {"block": "atr_stop", "params": {"period": atr_period, "mult": mult}}}
    if atr_target:
        exits["target"] = {"block": "atr_target", "params": {"period": 7, "mult": atr_target}}
    elif r:
        exits["target"] = {"block": "r_target", "params": {"r": r}}
    if trail:
        exits["trailing"] = {"block": "atr_trailing", "params": {"period": 10, "mult": trail}}
    if time_bars:
        exits["time"] = {"block": "time_exit", "params": {"bars": time_bars}}
    if opposite:
        exits["opposite"] = {"block": "opposite_signal"}
    spec = validate(
        {
            "name": "props",
            "symbol": symbol,
            "timeframe": timeframe.value,
            "direction": direction,
            "entry": {"block": "donchian_breakout", "params": {"period": period}},
            "exit": exits,
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
    # Also fixed inputs of tests/test_rules_version.py: a change here needs a re-pin there.
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
        atr_target=float(rng.choice([0.0, 0.0, 1.5, 4.0])),
        trail=float(rng.choice([0.0, 0.0, 1.0, 2.5])),
        time_bars=int(rng.choice([0, 0, 3, 12])),
        opposite=bool(rng.random() < 0.4),
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
    trail=st.sampled_from([0.0, 0.5, 1.0, 3.0]),
    time_bars=st.sampled_from([0, 1, 5, 20]),
    opposite=st.booleans(),
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

    # Positions never overlap. A new entry comes at least one bar after the last exit, except a
    # reversal: it enters the other side at the very open the last trade left at.
    assert np.all(t["entry_idx"][1:] >= t["exit_idx"][:-1])
    touching = t["entry_idx"][1:] == t["exit_idx"][:-1]
    assert np.all(t["side"][1:][touching] == -t["side"][:-1][touching])
    if not case["opposite"] or case["direction"] != "both":
        assert not touching.any()
    assert np.all(t["exit_idx"] >= t["entry_idx"])
    assert np.all(t["risk"] > 0)

    # Swap is reported on its own; the price part of R obeys the fill rules below.
    assert np.all(t["swap_r"] == 0)

    # A plain stop costs exactly 1R plus the exit slippage; a gap through the stop costs more.
    stop = reason == ExitReason.STOP
    assert np.allclose(t["r"][stop], -1 - slip_r[stop])
    gap = reason == ExitReason.STOP_GAP
    assert np.all(t["r"][gap] <= -1 - slip_r[gap] + eps)

    # A trailing exit is never worse than the initial stop; time and opposite exits leave at
    # an open, never past the stop. A time exit leaves exactly when its bars are up.
    trailed = reason == ExitReason.TRAIL
    assert np.all(t["r"][trailed] >= -1 - slip_r[trailed] - eps)
    timed = reason == ExitReason.TIME
    if case["time_bars"]:
        assert np.all(t["exit_idx"][timed] - t["entry_idx"][timed] == case["time_bars"])
        assert np.all(t["exit_idx"] - t["entry_idx"] <= case["time_bars"])
    else:
        assert not timed.any()
    if not case["trail"]:
        assert not trailed.any()
    if not case["opposite"]:
        assert not np.any(reason == ExitReason.OPPOSITE)

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


def reversal_case(seed):
    """ma_cross with an opposite-signal exit: every cross both closes and reverses.

    Also fixed inputs of tests/test_rules_version.py: a change here needs a re-pin there.
    """
    rng = np.random.default_rng(seed)
    bars = synthetic_bars(seed % 7, str(rng.choice(["GOLD", "EURUSD"])), Timeframe.M15)
    fast = int(rng.integers(3, 12))
    exits = {
        "stop": {"block": "atr_stop", "params": {"mult": float(rng.choice([2.0, 6.0]))}},
        "opposite": {"block": "opposite_signal"},
    }
    if rng.random() < 0.5:
        exits["time"] = {"block": "time_exit", "params": {"bars": int(rng.integers(5, 60))}}
    if rng.random() < 0.5:
        exits["trailing"] = {"block": "atr_trailing", "params": {"mult": 4.0}}
    filters = []
    if rng.random() < 0.5:  # a filter that blocks some reversals but never the exits
        filters = [{"block": "session_window", "params": {"zone": "london"}}]
    spec = validate(
        {
            "name": "reversals",
            "symbol": bars.symbol,
            "timeframe": "M15",
            "direction": str(rng.choice(["both", "both", "long"])),
            "entry": {"block": "ma_cross", "params": {"fast": fast, "slow": fast * 3}},
            "filters": filters,
            "exit": exits,
            "sizing": {"block": "fixed_risk", "params": {}},
        }
    )
    [combination] = expand_grid(spec)
    signals = compile_signals(combination, bars)
    cost = CostSnapshot(PROFILES[bars.symbol].point, 20, 1, swap_long_points=-50.0)
    use_m1 = rng.random() < 0.5
    m1 = synthetic_bars(seed % 7, bars.symbol, Timeframe.M1) if use_m1 else None
    return bars, signals, cost, m1, spec.direction


@pytest.mark.parametrize("seed", range(40))
def test_reversals_match_the_reference_twin(seed):
    bars, signals, cost, m1, direction = reversal_case(seed)

    trades = simulate(bars, signals, cost, m1=m1)
    expected = simulate_reference(bars, signals, cost, m1=m1)

    t = trades.records
    if direction == "long":  # a long-only Spec closes on the opposite signal, never reverses
        assert np.all(t["side"] == 1)
        assert not np.any(t["entry_idx"][1:] == t["exit_idx"][:-1])
    actual = trades.as_dicts()
    assert len(actual) == len(expected)
    for got, want in zip(actual, expected, strict=True):
        for field, value in want.items():
            if isinstance(value, float) and math.isnan(value):
                assert math.isnan(got[field]), field
            else:
                assert got[field] == pytest.approx(value, rel=1e-12, abs=1e-12), field


def test_the_reversal_cases_really_reverse():
    reversals = shorts = 0
    for seed in range(40):
        bars, signals, cost, m1, direction = reversal_case(seed)
        t = simulate(bars, signals, cost, m1=m1).records
        reversals += int((t["entry_idx"][1:] == t["exit_idx"][:-1]).sum())
        shorts += int((t["side"] == -1).sum()) if direction == "both" else 0

    assert reversals > 200
    assert shorts > 200  # ma_cross + opposite used to be long-only in practice
