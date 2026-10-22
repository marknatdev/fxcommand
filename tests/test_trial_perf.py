"""T10, spec §10: a 48-Combination Trial takes at most a minute on the dev laptop.

The named benchmark of tasks/plan.md: 11 research years of synthetic GOLD M15 with M1 settling;
donchian_breakout(period 20/40/60/80) × atr_stop(mult 1.5/2/2.5) × r_target(r 1.5/2/3/4), with
a London session_window filter; the full Gauntlet with 1,000 baseline runs, recorded in a
Ledger. Generating the synthetic data is not timed; fetching it through the market facade is.

Slow: run at checkpoints with `uv run pytest -m slow`.
"""

import time
from datetime import UTC, date, datetime

import pytest

from vibedesk.engine.costs import CostSnapshot
from vibedesk.gauntlet.trial import run_trial
from vibedesk.ledger import Ledger
from vibedesk.market.bars import Timeframe
from vibedesk.market.data import MarketData
from vibedesk.market.synthetic import SyntheticBarSource

pytestmark = pytest.mark.slow

BUDGET_SECONDS = 60.0
COST = CostSnapshot(0.01, 55, 1, swap_long_points=-83.83, swap_short_points=20.64)
GRID = {
    "name": "benchmark-grid",
    "symbol": "GOLD",
    "timeframe": "M15",
    "direction": "both",
    "entry": {"block": "donchian_breakout", "params": {"period": [20, 40, 60, 80]}},
    "filters": [{"block": "session_window", "params": {"zone": "london"}}],
    "exit": {
        "stop": {"block": "atr_stop", "params": {"mult": [1.5, 2.0, 2.5]}},
        "target": {"block": "r_target", "params": {"r": [1.5, 2.0, 3.0, 4.0]}},
    },
    "sizing": {"block": "fixed_risk", "params": {}},
}


def test_a_48_combination_trial_takes_at_most_a_minute(tmp_path):
    source = SyntheticBarSource(1, date(2014, 1, 1), date(2025, 1, 1))
    end = datetime(2025, 1, 1, tzinfo=UTC)
    source.fetch("GOLD", Timeframe.M15, end=end)  # generate the data before timing
    ledger = Ledger(tmp_path / "desk.db")
    market = MarketData(source, ledger)
    hypothesis = ledger.register_hypothesis(ledger.create_family("benchmark"), GRID)

    start = time.perf_counter()
    result = run_trial(ledger, market, hypothesis, COST)
    elapsed = time.perf_counter() - start
    print(f"48-Combination Trial: {elapsed:.1f} s, {result.verdict.verdict}")

    assert len(result.walk_forward.combinations) == 48
    assert len(result.baseline.net_r) == 1000
    assert elapsed <= BUDGET_SECONDS, f"{elapsed:.1f} s"
