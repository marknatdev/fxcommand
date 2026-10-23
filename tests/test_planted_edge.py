"""The planted edge (spec §9.6, M2 task T2): a known answer the Gauntlet must be able to find.

The fast tests check the mechanism: the drift lands only in the London opening hour and the
hours after it, both point the day's hidden way, and the hidden ups and downs balance per block.

The slow test is the calibration on the known-answer Spec. It asserts T2's acceptance and prints
the table recorded in docs/research/2026-10-22-planted-edge.md:

    uv run pytest tests/test_planted_edge.py -m slow -s

Other seeds or strengths print the same table without asserting, for example:

    uv run python tests/test_planted_edge.py --seeds 6-25
"""

from __future__ import annotations

import argparse
import statistics
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime

import numpy as np
import pytest

from vibedesk.blocks.compile import compile_signals
from vibedesk.blocks.spec import expand_grid, validate
from vibedesk.engine.costs import CostSnapshot
from vibedesk.engine.metrics import metrics
from vibedesk.engine.simulate import Signals, simulate
from vibedesk.market.bars import Timeframe
from vibedesk.market.server_time import DAY, server_offset
from vibedesk.market.sessions import local_minute_of_day
from vibedesk.market.synthetic import (
    EDGE_BLOCK_DAYS,
    PLANTED_EDGE,
    PLANTED_EDGE_RANGE,
    PLANTED_EDGE_SEEDS,
    SyntheticBarSource,
    SyntheticConfig,
)

KNOWN_ANSWER = {
    "name": "known-answer",
    "symbol": "GOLD",
    "timeframe": "M15",
    "direction": "both",
    "entry": {"block": "range_breakout", "params": {"session": "london_open", "range_bars": 4}},
    "exit": {
        "stop": {"block": "atr_stop", "params": {"mult": 1.5}},
        "target": {"block": "r_target", "params": {"r": 2.0}},
    },
    "sizing": {"block": "fixed_risk", "params": {}},
}
COST = CostSnapshot(point=0.01, spread_floor_points=55, slippage_points=1)
HOLDOUT = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp())
BLIND = int(datetime(2017, 1, 1, tzinfo=UTC).timestamp())  # the walk-forward's blind years
COIN_RUNS = 20


# --- Mechanism ------------------------------------------------------------------------------

# Ten server weeks, ending after the US switch (10 March) and before the UK one (31 March), so
# the London open sits an hour later on the server clock in the last week.
MECH_START, MECH_END = date(2019, 1, 7), date(2019, 3, 18)


@pytest.fixture(scope="module")
def drift():
    """Per M1 bar: London minutes since 08:00, server day, and the edge's added log return.

    With no missing minutes, the edge-on and edge-off bars share every draw, so the difference
    of their log closes is exactly the edge's drift, up to the point-grid rounding.
    """
    whole = SyntheticConfig(missing_bar_prob=0.0)
    walk = SyntheticBarSource(3, MECH_START, MECH_END, whole).fetch("GOLD", Timeframe.M1)
    edge_config = replace(PLANTED_EDGE, missing_bar_prob=0.0)
    edge = SyntheticBarSource(3, MECH_START, MECH_END, edge_config).fetch("GOLD", Timeframe.M1)
    added = np.diff(np.log(edge.close) - np.log(walk.close), prepend=0.0)
    since_open = local_minute_of_day(walk.time, "london") - 8 * 60
    day = np.unique((walk.time + server_offset(walk.time)) // DAY, return_inverse=True)[1]
    return since_open, day, added


def _day_moves(drift, lo, hi):
    since_open, day, added = drift
    inside = (since_open >= lo) & (since_open < hi)
    return np.bincount(day[inside], weights=added[inside], minlength=day.max() + 1)


def test_drift_stays_inside_the_opening_hour_and_the_follow_hours(drift):
    since_open, _, added = drift
    edge_hours = (since_open >= 0) & (since_open < 60 * (1 + PLANTED_EDGE.follow_hours))

    assert np.abs(added[~edge_hours]).max() < 5e-5  # only point-grid rounding


def test_the_opening_hour_and_the_follow_hours_point_the_same_hidden_way(drift):
    reveal = _day_moves(drift, 0, 60)
    follow = _day_moves(drift, 60, 60 * (1 + PLANTED_EDGE.follow_hours))
    mornings = reveal != 0  # the last server day ends before the London open

    assert mornings.sum() == 50
    assert np.array_equal(np.sign(reveal[mornings]), np.sign(follow[mornings]))
    # In hourly volatilities, the opening hour moves `reveal` and each follow hour `follow`. All
    # those hours share the day's volatility (server 10:00-15:00), so the ratio is exact.
    ratio = follow[mornings] / reveal[mornings]
    expected = PLANTED_EDGE.follow * PLANTED_EDGE.follow_hours / PLANTED_EDGE.reveal
    assert ratio == pytest.approx(expected, rel=0.02)


def test_hidden_ups_and_downs_balance_in_every_full_block(drift):
    direction = np.sign(_day_moves(drift, 0, 60))
    full = len(direction) // EDGE_BLOCK_DAYS * EDGE_BLOCK_DAYS

    assert full == 2 * EDGE_BLOCK_DAYS
    assert np.all(direction[:full].reshape(-1, EDGE_BLOCK_DAYS).sum(axis=1) == 0)


def test_known_answer_spec_earns_the_edge_and_not_the_walk():
    start, end = date(2019, 1, 1), date(2021, 1, 1)
    walk = _profit_factor(SyntheticBarSource(1, start, end))
    edge = _profit_factor(SyntheticBarSource(1, start, end, PLANTED_EDGE))

    assert walk < 1.0
    assert edge > 1.3


def _profit_factor(source):
    bars, m1 = source.fetch("GOLD", Timeframe.M15), source.fetch("GOLD", Timeframe.M1)
    [combination] = expand_grid(validate(KNOWN_ANSWER))
    trades = simulate(bars, compile_signals(combination, bars), COST, m1=m1)
    return metrics(trades, bars).profit_factor


# --- Calibration ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Run:
    """Profit factors after costs of one Spec run, by window and by entry year."""

    research: float  # entries 2014-2024
    blind: float  # entries 2017-2024, the walk-forward's blind years
    holdout: float  # entries from 2025
    years: dict[int, float]
    cost_r: dict[int, float]  # mean spread and slippage per trade, in R


@dataclass(frozen=True)
class Calibration:
    seed: int
    walk: Run
    edge: Run
    stress: Run  # the edge at twice the spread
    coin_research: list[float]  # random sides on the edge Spec's own signal bars
    coin_holdout: list[float]


def calibrate(seed: int, config: SyntheticConfig = PLANTED_EDGE) -> Calibration:
    walk, _ = _runs(seed, SyntheticConfig())
    edge, extra = _runs(seed, config, stress=True, coins=COIN_RUNS)
    return Calibration(seed, walk[0], edge[0], edge[1], *extra)


def _runs(seed, config, stress=False, coins=0):
    source = SyntheticBarSource(seed, *PLANTED_EDGE_RANGE, config)
    bars, m1 = source.fetch("GOLD", Timeframe.M15), source.fetch("GOLD", Timeframe.M1)
    [combination] = expand_grid(validate(KNOWN_ANSWER))
    signals = compile_signals(combination, bars)
    runs = [_run(simulate(bars, signals, COST, m1=m1), bars)]
    if stress:
        runs.append(_run(simulate(bars, signals, COST.stressed(), m1=m1), bars))
    # Coin flips at the same bars with the same exits: the same hours, a random side.
    fired = signals.long_entry | signals.short_entry
    rng = np.random.default_rng([seed, 99])
    coin_research, coin_holdout = [], []
    for _ in range(coins):
        side = rng.random(len(bars)) < 0.5
        coin = Signals(fired & side, fired & ~side, signals.stop_dist, signals.target_dist)
        run = _run(simulate(bars, coin, COST, m1=m1), bars, by_year=False)
        coin_research.append(run.research)
        coin_holdout.append(run.holdout)
    return runs, (coin_research, coin_holdout)


def _run(trades, bars, by_year=True):
    entry = trades["entry_time"]
    year = np.array([datetime.fromtimestamp(int(t), UTC).year for t in entry])

    def pf(mask):
        return metrics(trades[mask], bars).profit_factor

    years = np.unique(year) if by_year else []
    return Run(
        research=pf(entry < HOLDOUT),
        blind=pf((entry >= BLIND) & (entry < HOLDOUT)),
        holdout=pf(entry >= HOLDOUT),
        years={int(y): pf(year == y) for y in years},
        cost_r={int(y): float(trades["cost_r"][year == y].mean()) for y in years},
    )


def table(rows: list[Calibration]) -> str:
    lines = [
        "seed | walk PF: 2014-24 2025-26 | edge PF: 2014-24 2025-26 | 2x spread: 2014-24 "
        "2017-24 2025-26 | coin PF mean/max: 2014-24 2025-26"
    ]
    for c in rows:
        coin_r, coin_h = c.coin_research, c.coin_holdout
        lines.append(
            f"{c.seed:4d} | {c.walk.research:16.2f} {c.walk.holdout:7.2f} | "
            f"{c.edge.research:16.2f} {c.edge.holdout:7.2f} | "
            f"{c.stress.research:18.2f} {c.stress.blind:7.2f} {c.stress.holdout:7.2f} | "
            f"{statistics.mean(coin_r):23.2f}/{max(coin_r):.2f} "
            f"{statistics.mean(coin_h):.2f}/{max(coin_h):.2f}"
        )
    years = sorted(rows[0].edge.years)
    lines += ["", "edge PF by entry year (mean cost in R)", "seed | " + " ".join(map(str, years))]
    for c in rows:
        cells = (f"{c.edge.years[y]:.2f} ({c.edge.cost_r[y]:.2f})" for y in years)
        lines.append(f"{c.seed:4d} | " + " ".join(cells))
    return "\n".join(lines)


@pytest.mark.slow
@pytest.mark.skipif(
    "'slow' not in config.getoption('markexpr')", reason="slow: uv run pytest -m slow"
)
def test_calibrated_edge_meets_the_known_answers(capsys):
    rows = [calibrate(seed) for seed in PLANTED_EDGE_SEEDS]
    with capsys.disabled():
        print("\n" + table(rows))

    for c in rows:
        assert c.walk.research < 1.05, f"seed {c.seed}: the random walk earns"
        assert c.edge.research >= 1.3, f"seed {c.seed}: edge too weak in 2014-2024"
        assert c.edge.holdout >= 1.3, f"seed {c.seed}: edge too weak in 2025-2026"
        assert max(c.coin_research) < 1.05, f"seed {c.seed}: coin flips share the edge"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Print the planted-edge calibration table.")
    parser.add_argument("--seeds", default="1-5", help="a range such as 6-25")
    parser.add_argument("--reveal", type=float, default=PLANTED_EDGE.reveal)
    parser.add_argument("--follow", type=float, default=PLANTED_EDGE.follow)
    parser.add_argument("--hours", type=int, default=PLANTED_EDGE.follow_hours)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    first, _, last = args.seeds.partition("-")
    seeds = range(int(first), int(last or first) + 1)
    config = SyntheticConfig(reveal=args.reveal, follow=args.follow, follow_hours=args.hours)
    with ProcessPoolExecutor(args.workers) as pool:
        print(table(list(pool.map(calibrate, seeds, [config] * len(seeds)))))
