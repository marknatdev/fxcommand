"""The trade rules match their RULES_VERSION (src/vibedesk/rules.py).

Each part of the rules is run on fixed synthetic data and its output hashed: every Block, the
engine on the reference-twin cases, one small Trial end to end (walk-forward, stitching, the
Coin-Flip Baseline and every check with its threshold) and the thresholds themselves. A pinned
part whose output changes fails here by name. If the change is meant, bump RULES_VERSION, note
it in rules.py and re-pin; a Holdout then refuses every Trial judged under the old rules.

A new Block is only missing a pin: pin it without a bump, since no existing Spec changed.
Floats are hashed to 9 significant digits, so last-bit noise from a library update is not a
rule change. Of the Trial, only what the rules decide is hashed, so a new field in its record
is no rule change either. Re-pin with: uv run python tests/test_rules_version.py

The fixed inputs (the synthetic bars, the look-ahead parameters, the engine cases) are pinned
too, as `inputs:*`. They are no rules, but changing them moves every other part. Change them in
a commit of their own and re-pin there without a bump, so the rule parts are always checked
against unchanged inputs.
"""

import hashlib
import math
from datetime import date
from functools import cache

import numpy as np
import pytest
from test_engine_props import random_case, reversal_case, run_case, synthetic_bars
from test_lookahead import PARAMS, base_bars, outputs

from vibedesk.blocks.registry import LIBRARY
from vibedesk.engine.costs import CostSnapshot
from vibedesk.engine.simulate import simulate
from vibedesk.gauntlet import baseline, prosecutor, trial, verdict, walkforward
from vibedesk.ledger import Ledger
from vibedesk.market import grant
from vibedesk.market.bars import Timeframe
from vibedesk.market.data import MarketData
from vibedesk.market.synthetic import SyntheticBarSource
from vibedesk.rules import RULES_VERSION

PINNED_FOR = 1  # the RULES_VERSION these pins were taken under
PINNED = {
    "inputs:bars": "cdc3af79776dc042",
    "inputs:cases": "806038e6be1488f5",
    "block:adx": "5e952974c42ee9a3",
    "block:atr_regime": "10835cb277ffbc43",
    "block:atr_stop": "91654392e1e2e447",
    "block:atr_target": "2e72c5f1f3a418e6",
    "block:atr_trailing": "e8d97ad3ab885aaf",
    "block:bollinger_touch": "108ee7dbf020344f",
    "block:day_of_week": "2d346bfe98b32d75",
    "block:donchian_breakout": "23a931a9f8301022",
    "block:fixed_risk": "8fdad37b2f3cb0df",
    "block:ma_cross": "f343a287521318f1",
    "block:ma_slope": "f9e169bfb82e5b2a",
    "block:opposite_signal": "c7c7e17e8bce7431",
    "block:pullback_to_ma": "9f7c5cc670480f96",
    "block:r_target": "f4fa348355d8ee83",
    "block:range_breakout": "a19a88ccb4e7ee06",
    "block:rsi_threshold": "7b4e424221cb1e91",
    "block:session_window": "ba97e8a98c5fabff",
    "block:time_exit": "0805495db21c14e4",
    "engine:random": "7081790f4b974fc2",
    "engine:reversal": "eaa5920b37d4ed55",
    "trial": "5ba7d911d9878bc8",
    "thresholds": "69aa95512d63496e",
}

LOOKAHEAD_SEEDS = (0, 1)
ENGINE_CASES = 40
REVERSAL_CASES = 20
WALK_FORWARD_KEYS = (
    "years", "choices", "holdout_choice", "train_trades", "train_pf", "blind_trades",
    "blind_net_r", "blind_sharpe", "blind_pf", "metrics", "fold_net_r", "stress_metrics",
    "stress_fold_net_r", "daily_r", "trade_list",
)  # fmt: skip
THRESHOLDS = {
    "BLIND_YEARS": walkforward.BLIND_YEARS,
    "TRAIN_YEARS": walkforward.TRAIN_YEARS,
    "MIN_TRAIN_TRADES": walkforward.MIN_TRAIN_TRADES,
    "RUNS": baseline.RUNS,
    "MIN_PROFIT_FACTOR": verdict.MIN_PROFIT_FACTOR,
    "MIN_TRADES": verdict.MIN_TRADES,
    "POSITIVE_FOLD_SHARE": verdict.POSITIVE_FOLD_SHARE,
    "MIN_STRESS_PROFIT_FACTOR": verdict.MIN_STRESS_PROFIT_FACTOR,
    "BASELINE_PERCENTILE": verdict.BASELINE_PERCENTILE,
    "MIN_DEFLATED_SHARPE": verdict.MIN_DEFLATED_SHARPE,
    "MAX_PBO": verdict.MAX_PBO,
    "MAX_TOP5_SHARE": prosecutor.MAX_TOP5_SHARE,
    "MIN_NEIGHBOUR_PF": prosecutor.MIN_NEIGHBOUR_PF,
    "MIN_SIDE_PF": prosecutor.MIN_SIDE_PF,
    "MIN_REGIME_PF": prosecutor.MIN_REGIME_PF,
    "MIN_SPLIT_TRADES": prosecutor.MIN_SPLIT_TRADES,
    "ATR_PERIOD": prosecutor.ATR_PERIOD,
    "MIN_HOLDOUT_TRADES": trial.MIN_HOLDOUT_TRADES,
    "MIN_HOLDOUT_PROFIT_FACTOR": trial.MIN_HOLDOUT_PROFIT_FACTOR,
    "HOLDOUT_START": grant.HOLDOUT_START.isoformat(),
}
TRIAL_SPEC = {
    "name": "rules-fingerprint",
    "symbol": "GOLD",
    "timeframe": "H1",
    "direction": "both",
    "entry": {"block": "donchian_breakout", "params": {"period": [20, 40]}},
    "filters": [{"block": "session_window", "params": {"zone": "london"}}],
    "exit": {
        "stop": {"block": "atr_stop", "params": {"mult": 1.5}},
        "target": {"block": "r_target", "params": {"r": 2.0}},
        "opposite": {"block": "opposite_signal"},
    },
    "sizing": {"block": "fixed_risk", "params": {}},
}


def canonical(value) -> str:
    """A stable text form: floats to 9 significant digits, near-zero as 0."""
    if isinstance(value, np.ndarray) and value.dtype.names:
        return canonical({name: value[name] for name in value.dtype.names})
    if isinstance(value, np.ndarray) and value.ndim == 0:
        return canonical(value.item())
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda item: str(item[0]))
        return "{" + ",".join(f"{k}:{canonical(v)}" for k, v in items) + "}"
    if isinstance(value, list | tuple | np.ndarray):
        return "[" + ",".join(canonical(v) for v in value) + "]"
    if isinstance(value, bool | np.bool_):
        return str(bool(value))
    if isinstance(value, int | np.integer):
        return str(int(value))
    if isinstance(value, float | np.floating):
        x = float(value)
        if math.isnan(x):
            return "nan"
        return "0" if abs(x) < 1e-9 else format(x, ".9g")
    return repr(value)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()[:16]


def bars_digest(all_bars) -> str:
    """The exact bytes of every bar array: inputs are generated, so they must match exactly."""
    sha = hashlib.sha256()
    for bars in all_bars:
        for name in ("time", "open", "high", "low", "close", "spread"):
            sha.update(np.ascontiguousarray(getattr(bars, name)).tobytes())
    return sha.hexdigest()[:16]


@cache
def trial_source() -> SyntheticBarSource:
    return SyntheticBarSource(1, date(2021, 1, 1), date(2025, 1, 1))


def input_parts() -> dict[str, str]:
    lookahead = [base_bars(seed) for seed in LOOKAHEAD_SEEDS]
    engine = []
    for seed in range(ENGINE_CASES):
        case = random_case(seed)
        engine.append(synthetic_bars(seed, case["symbol"], case["timeframe"]))
        if case["use_m1"]:
            engine.append(synthetic_bars(seed, case["symbol"], Timeframe.M1))
    for seed in range(REVERSAL_CASES):
        bars, _, _, m1, _ = reversal_case(seed)
        engine += [bars] if m1 is None else [bars, m1]
    trial_bars = [trial_source().fetch("GOLD", tf) for tf in (Timeframe.H1, Timeframe.M1)]
    return {
        "inputs:bars": bars_digest([*lookahead, *engine, *trial_bars]),
        "inputs:cases": digest(
            [PARAMS, [random_case(seed) for seed in range(ENGINE_CASES)], TRIAL_SPEC]
        ),
    }


def block_parts() -> dict[str, str]:
    parts = {}
    for block in sorted(LIBRARY.all(), key=lambda b: b.name):
        results = []
        for params in PARAMS.get(block.name, [{}]):
            for seed in LOOKAHEAD_SEEDS:
                results.append(outputs(block, block.params(**params), base_bars(seed)))
        parts[f"block:{block.name}"] = digest(results)
    return parts


def engine_parts() -> dict[str, str]:
    plain = [run_case(**random_case(seed))[-1].records for seed in range(ENGINE_CASES)]
    reversals = []
    for seed in range(REVERSAL_CASES):
        bars, signals, cost, m1, _ = reversal_case(seed)
        reversals.append(simulate(bars, signals, cost, m1=m1).records)
    return {"engine:random": digest(plain), "engine:reversal": digest(reversals)}


def trial_part() -> str:
    with Ledger(":memory:") as ledger:
        family = ledger.create_family("rules fingerprint")
        hypothesis = ledger.register_hypothesis(family, TRIAL_SPEC)
        cost = CostSnapshot(0.01, 17, 1, swap_long_points=-83.83, swap_short_points=20.64)
        result = trial.run_trial(
            ledger,
            MarketData(trial_source(), ledger),
            hypothesis,
            cost,
            years=(2023, 2024),
            train_years=2,
            runs=100,
        )
    return digest(decided_by_rules(result.record))


def decided_by_rules(record: dict) -> dict:
    """What the rules decide in a Trial record, without its ids, versions or display fields."""
    wf, coin = record["walk_forward"], record["baseline"]
    return {
        "verdict": record["verdict"],
        "closest_miss": record["closest_miss"],
        "checks": [{k: c[k] for k in ("name", "value", "threshold", "status")} for c in
                   record["checks"]],
        "walk_forward": {k: wf[k] for k in WALK_FORWARD_KEYS},
        "baseline": {k: coin[k] for k in ("p95", "percentile", "strategy_net_r", "net_r",
                                          "fold_trades")},
        "deflated_sharpe": record["deflated_sharpe"],
        "pbo": record["pbo"],
        "holdout_rate": record["holdout"]["rate_per_year"],
    }  # fmt: skip


def fingerprints() -> dict[str, str]:
    return {
        **input_parts(),
        **block_parts(),
        **engine_parts(),
        "trial": trial_part(),
        "thresholds": digest(THRESHOLDS),
    }


@pytest.fixture(scope="module")
def current():
    return fingerprints()


def test_the_pins_belong_to_todays_rules():
    assert PINNED_FOR == RULES_VERSION, (
        f"RULES_VERSION is {RULES_VERSION} but the pins were taken under {PINNED_FOR}: re-pin"
        " every part (uv run python tests/test_rules_version.py) and set PINNED_FOR"
    )


def test_every_part_has_a_pin(current):
    missing = sorted(set(current) - set(PINNED))
    assert not missing, (
        f"no pin for {missing}. A new Block changes no existing Spec: pin it without a bump"
        " (uv run python tests/test_rules_version.py prints every pin)"
    )


def test_no_rule_changed_without_a_bump(current):
    changed = sorted(name for name in PINNED if current.get(name) != PINNED[name])
    inputs = [name for name in changed if name.startswith("inputs:")]
    assert not inputs, (
        f"the fixed inputs changed: {inputs}. That is no rule change, but it moves every part:"
        " change inputs in a commit of their own and re-pin there without a bump"
        " (uv run python tests/test_rules_version.py)"
    )
    assert not changed, (
        f"these parts of the trade rules changed: {changed}. If that is meant, bump"
        " RULES_VERSION in src/vibedesk/rules.py, note the change there and re-pin"
        " (uv run python tests/test_rules_version.py); otherwise it is a bug"
    )


if __name__ == "__main__":
    for name, value in fingerprints().items():
        print(f'    "{name}": "{value}",')
