"""T10, spec §9.6: the Gauntlet's known answers, through the real Trial runner and Holdout.

A random walk has no edge, so it must almost never be a CANDIDATE. The planted edge (T2) is
real, so the known-answer Spec must find it and keep it through the Holdout. Thresholds were
fixed in tasks/plan.md before any of these ran; the random-walk seeds (101-120) were never
used to calibrate anything. The Spread Floor is 17 points, real GOLD's cost relative to price
on the synthetic price level (owner's decision, 2026-10-22).

Each seed keeps only a small summary: a Trial result holds its prepared markets and with them
twelve years of minute bars, and 25 of those at once would not fit in the laptop's memory.

Slow: run at checkpoints with `uv run pytest -m slow`.
"""

from datetime import UTC, datetime

import pytest

from vibedesk.engine.costs import CostSnapshot
from vibedesk.gauntlet.trial import PASS, request_holdout, run_holdout, run_trial
from vibedesk.gauntlet.verdict import CANDIDATE
from vibedesk.ledger import Ledger
from vibedesk.market.data import MarketData
from vibedesk.market.synthetic import (
    PLANTED_EDGE,
    PLANTED_EDGE_RANGE,
    PLANTED_EDGE_SEEDS,
    SyntheticBarSource,
    SyntheticConfig,
)

pytestmark = pytest.mark.slow

WALK_SEEDS = tuple(range(101, 121))
MAX_WALK_CANDIDATES = 1
MIN_EDGE_CANDIDATES = 4
NOW = datetime(2026, 10, 1, tzinfo=UTC)
COST = CostSnapshot(0.01, 17, 1, swap_long_points=-83.83, swap_short_points=20.64)
SPEC = {
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


def known_answer(path, seed, config, holdout=False):
    """One production-shaped Trial (2017-2024 blind years, M1, 1,000 runs) in a fresh Ledger,
    and its Holdout when asked and the Trial is a CANDIDATE. Returns a summary only."""
    source = SyntheticBarSource(seed, *PLANTED_EDGE_RANGE, config=config)
    with Ledger(path) as ledger:
        market = MarketData(source, ledger)
        hypothesis = ledger.register_hypothesis(ledger.create_family("known answer"), SPEC)
        result = run_trial(ledger, market, hypothesis, COST)
        summary = {
            "verdict": result.verdict.verdict,
            "profit_factor": result.walk_forward.metrics.profit_factor,
            "baseline": result.baseline.percentile,
            "failed": [c.name for c in result.verdict.failed()],
            "holdout": None,
        }
        if holdout and summary["verdict"] == CANDIDATE:
            grant = ledger.approve_holdout(request_holdout(ledger, hypothesis, now=NOW))
            spent = run_holdout(ledger, market, grant, now=NOW)
            summary["holdout"] = (spent.verdict, len(spent.trades), spent.metrics.profit_factor)
    line = f"seed {seed:3d}: {summary['verdict']:9s} PF {summary['profit_factor']:.2f}"
    line += f" baseline {summary['baseline']:.2f} failed {','.join(summary['failed']) or '-'}"
    if summary["holdout"]:
        verdict, trades, pf = summary["holdout"]
        line += f" | Holdout {verdict}: {trades} trades, PF {pf:.2f}"
    print(line)
    return summary


@pytest.fixture(scope="module")
def walk(tmp_path_factory):
    return {
        seed: known_answer(
            tmp_path_factory.mktemp(f"walk{seed}") / "desk.db", seed, SyntheticConfig()
        )
        for seed in WALK_SEEDS
    }


@pytest.fixture(scope="module")
def edge(tmp_path_factory):
    return {
        seed: known_answer(
            tmp_path_factory.mktemp(f"edge{seed}") / "desk.db", seed, PLANTED_EDGE, holdout=True
        )
        for seed in PLANTED_EDGE_SEEDS
    }


def test_a_random_walk_is_almost_never_a_candidate(walk):
    candidates = [seed for seed, s in walk.items() if s["verdict"] == CANDIDATE]

    assert len(candidates) <= MAX_WALK_CANDIDATES, f"random-walk CANDIDATEs: {candidates}"


def test_the_planted_edge_is_found(edge):
    found = [seed for seed, s in edge.items() if s["verdict"] == CANDIDATE]

    assert len(found) >= MIN_EDGE_CANDIDATES, f"planted-edge CANDIDATEs: {found}"


def test_every_planted_candidate_passes_its_holdout(edge):
    holdouts = {seed: s["holdout"][0] for seed, s in edge.items() if s["holdout"]}

    assert holdouts
    assert set(holdouts.values()) == {PASS}, holdouts
