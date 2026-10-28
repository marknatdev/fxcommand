"""Tests for Holdout and Trial carry-forwards (spec §6.3, tasks/plan.md, CCC-55).

Tests cover:
1. Grant reusability: HoldoutGrant is reusable across multiple fetches until spend is recorded.
2. Binding: Each Holdout Request is bound to its candidate Trial ID.
3. Per-family trial serialization: Per-family locks ensure sequential execution and consistent N.
4. Holdout Gate: Refuses non-CANDIDATE, changed rules, or insufficient trades.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from vibedesk.app.desk import Desk
from vibedesk.app.research import EXAMPLE
from vibedesk.blocks.spec import expand_grid, validate
from vibedesk.engine.costs import CostSnapshot
from vibedesk.gauntlet.trial import (
    CANDIDATE,
    HoldoutRefused,
    _current_candidate,
)
from vibedesk.market.bars import Timeframe
from vibedesk.market.cache import record_import, write_chunk
from vibedesk.market.costs import CostMeasurement, record_cost
from vibedesk.market.data import HoldoutLocked
from vibedesk.market.grant import HOLDOUT_START, HoldoutGrant
from vibedesk.market.synthetic import SyntheticBarSource
from vibedesk.rules import RULES_VERSION


def setup_cache(root: Path) -> None:
    source = SyntheticBarSource(1, date(2013, 12, 1), date(2026, 1, 1))
    for tf in (Timeframe.M15, Timeframe.H1):
        bars = source.fetch("GOLD", tf)
        for year in range(2013, 2026):
            s = datetime(year, 1, 1, tzinfo=UTC).timestamp()
            e = datetime(year + 1, 1, 1, tzinfo=UTC).timestamp()
            write_chunk(root, bars.between(int(s), int(e)), year)
    record_import(root, "GOLD", datetime(2026, 1, 1, tzinfo=UTC))
    record_cost(
        root,
        CostMeasurement(
            "GOLD", "2026-10-23T00:00:00+00:00", 0.01, 30.0, 35.0, 1000, -50.0, 10.0, 3
        ),
    )


def record_dummy_trial(
    desk: Desk,
    hypothesis_id: int,
    verdict: str = CANDIDATE,
    rules_version: int = RULES_VERSION,
    rate_per_year: float = 50.0,
) -> int:
    """Helper to record a trial directly into Ledger for carry-forward tests."""
    hyp = desk.ledger.hypothesis(hypothesis_id)
    cost = CostSnapshot("GOLD", 30.0, 0.01, 1.0, 0.0, 0.0, 3)
    bars = desk.market.bars(
        "GOLD",
        Timeframe.H1,
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 1, tzinfo=UTC),
    )
    combos = expand_grid(validate(hyp.spec_json))
    combinations = [(c, 1.5) for c in combos]
    import numpy as np

    np.random.seed(42)
    daily_r_list = np.random.normal(0.2, 0.5, 1000).tolist()
    result_data = {
        "verdict": verdict,
        "rules_version": rules_version,
        "walk_forward": {
            "daily_r": {"day_start": list(range(1000)), "r": daily_r_list},
            "holdout_choice": 0,
        },
        "holdout": {"rate_per_year": rate_per_year},
    }
    return desk.ledger.record_trial(
        hypothesis_id,
        combinations=combinations,
        verdict=verdict,
        closest_miss=None if verdict == CANDIDATE else {"check": "profit_factor", "ratio": 0.9},
        cost=cost,
        bars=bars,
        rules_version=rules_version,
        result_json=json.dumps(result_data),
    )


def test_grant_reusable_until_spend_recorded(tmp_path: Path):
    """A HoldoutGrant is reusable across multiple fetches until spend is recorded (CCC-55)."""
    setup_cache(tmp_path)
    desk = Desk(tmp_path)
    fam_id = desk.ledger.create_family("Reusable Grant Family")
    hyp_id = desk.ledger.register_hypothesis(fam_id, EXAMPLE)
    record_dummy_trial(desk, hyp_id, verdict=CANDIDATE)

    req_id = desk.request_holdout(hyp_id)
    grant = desk.approve_holdout(req_id)
    assert isinstance(grant, HoldoutGrant)

    # 1. Grant verifies successfully
    assert desk.ledger.verify(grant, "GOLD") is True

    # 2. Grant can fetch bars in the Holdout period multiple times (reusable)
    h_start = HOLDOUT_START
    h_mid = datetime(2025, 6, 1, tzinfo=UTC)
    bars1 = desk.market.bars("GOLD", Timeframe.H1, h_start, h_mid, grant=grant)
    assert len(bars1) > 0

    bars2 = desk.market.bars(
        "GOLD",
        Timeframe.H1,
        h_mid,
        datetime(2025, 12, 1, tzinfo=UTC),
        grant=grant,
    )
    assert len(bars2) > 0

    # 3. Grant still verifies after multiple fetches
    assert desk.ledger.verify(grant, "GOLD") is True

    # 4. Record spend
    desk.ledger.record_holdout(grant, "PASS", json.dumps({"trades": 50, "profit_factor": 1.5}))

    # 5. After spend, verification immediately fails and subsequent fetches are refused
    assert desk.ledger.verify(grant, "GOLD") is False
    with pytest.raises(HoldoutLocked):
        desk.market.bars("GOLD", Timeframe.H1, h_start, h_mid, grant=grant)


def test_holdout_request_bound_to_trial_id(tmp_path: Path):
    """Each Holdout Request is bound to the candidate Trial ID it was made from (CCC-55)."""
    setup_cache(tmp_path)
    desk = Desk(tmp_path)
    fam_id = desk.ledger.create_family("Binding Test Family")
    hyp_id = desk.ledger.register_hypothesis(fam_id, EXAMPLE)

    trial_id_1 = record_dummy_trial(desk, hyp_id, verdict=CANDIDATE)

    # Request holdout binds to trial_id_1
    req_id = desk.request_holdout(hyp_id)
    assert req_id > 0
    bound_trial = desk.holdout_request_trial(req_id)
    assert bound_trial == trial_id_1

    # Verify persistence across a fresh Desk instance on the same directory
    desk2 = Desk(tmp_path)
    assert desk2.holdout_request_trial(req_id) == trial_id_1


def test_trial_serialization_per_family(tmp_path: Path):
    """Per-family locks serialize trials for the same family to freeze N consistently (CCC-55)."""
    desk = Desk(tmp_path)
    fam1 = desk.ledger.create_family("Family 1")
    fam2 = desk.ledger.create_family("Family 2")

    lock1_a = desk.family_lock(fam1)
    lock1_b = desk.family_lock(fam1)
    lock2 = desk.family_lock(fam2)

    # Same family shares the exact same lock
    assert lock1_a is lock1_b
    # Different family gets a distinct lock
    assert lock1_a is not lock2

    execution_order = []

    def task_family_1(task_id: int, delay: float):
        with desk.family_lock(fam1):
            execution_order.append(f"start_f1_{task_id}")
            time.sleep(delay)
            execution_order.append(f"end_f1_{task_id}")

    # Launch two concurrent tasks on family 1
    t1 = threading.Thread(target=task_family_1, args=(1, 0.05))
    t2 = threading.Thread(target=task_family_1, args=(2, 0.01))

    t1.start()
    time.sleep(0.01)  # ensure t1 acquires lock first
    t2.start()

    t1.join()
    t2.join()

    # Must execute sequentially: start_1 -> end_1 -> start_2 -> end_2
    assert execution_order == [
        "start_f1_1",
        "end_f1_1",
        "start_f1_2",
        "end_f1_2",
    ]


def test_holdout_gate_refuses_invalid_trials(tmp_path: Path):
    """Holdout gate checks in §6.3: CANDIDATE, RULES_VERSION, expected trades >= 30."""
    setup_cache(tmp_path)
    desk = Desk(tmp_path)
    fam_id = desk.ledger.create_family("Gate Checks Family")
    hyp_id = desk.ledger.register_hypothesis(fam_id, EXAMPLE)

    # 1. No trial -> refused
    with pytest.raises(HoldoutRefused, match="NOT_CANDIDATE"):
        _current_candidate(desk.ledger, hyp_id)

    # 2. REJECT trial -> refused
    record_dummy_trial(desk, hyp_id, verdict="REJECT")
    with pytest.raises(HoldoutRefused, match="NOT_CANDIDATE"):
        desk.request_holdout(hyp_id)

    # 3. Outdated rules version -> refused
    hyp2_id = desk.ledger.register_hypothesis(fam_id, EXAMPLE)
    record_dummy_trial(desk, hyp2_id, verdict=CANDIDATE, rules_version=RULES_VERSION - 1)
    with pytest.raises(HoldoutRefused, match="RULES_CHANGED"):
        desk.request_holdout(hyp2_id)

    # 4. Insufficient expected trades -> refused
    hyp3_id = desk.ledger.register_hypothesis(fam_id, EXAMPLE)
    record_dummy_trial(desk, hyp3_id, verdict=CANDIDATE, rate_per_year=1.0)
    with pytest.raises(HoldoutRefused, match="INSUFFICIENT_TRADES"):
        desk.request_holdout(hyp3_id, now=datetime(2025, 2, 1, tzinfo=UTC))
