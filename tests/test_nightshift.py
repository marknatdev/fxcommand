"""Unit and integration tests for Night Shift (spec §6, ADR 0011, Card CCC-75)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vibedesk.app.llm import ScriptedLLM, Turn
from vibedesk.app.research import EXAMPLE
from vibedesk.blocks.spec import validate
from vibedesk.desk.budget import TokenBudget
from vibedesk.ledger import Ledger
from vibedesk.nightshift import (
    NightShiftConfig,
    NightShiftState,
    compile_morning_brief,
    format_morning_brief,
    run_night_shift,
)
from vibedesk.nightshift.runner import generate_family_variations
from vibedesk.notify import NoticeKind, RecordingNotifier


class FakeDesk:
    """Deterministic fake desk for fast testing of Night Shift loop and limits."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.ledger = Ledger(root / "desk.db")
        self.run_calls: list[tuple[str, dict[str, Any]]] = []
        self.verdicts_to_return: list[str] = []

    def list_families(self) -> list[dict[str, Any]]:
        with self.ledger._lock:
            rows = self.ledger._db.execute(
                """
                SELECT f.id, f.idea, f.created_at,
                       (SELECT COUNT(*) FROM hypotheses h
                        WHERE h.family_id = f.id) as hypothesis_count,
                       (SELECT COUNT(*) FROM trials t
                        JOIN hypotheses h ON t.hypothesis_id = h.id
                        WHERE h.family_id = f.id) as trial_count
                FROM families f ORDER BY f.id DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def get_family(self, family_id: int) -> dict[str, Any]:
        fam = self.ledger.family(family_id)
        budget = self.ledger.budget(family_id)
        trial_count = self.ledger.trial_count(family_id)
        with self.ledger._lock:
            hyp_rows = self.ledger._db.execute(
                "SELECT id, family_id, entry_block, symbol, timeframe, spec_json, created_at "
                "FROM hypotheses WHERE family_id = ? ORDER BY id ASC",
                (family_id,),
            ).fetchall()
            hypotheses = [
                {
                    "id": h["id"],
                    "family_id": h["family_id"],
                    "entry_block": h["entry_block"],
                    "symbol": h["symbol"],
                    "timeframe": h["timeframe"],
                    "spec": json.loads(h["spec_json"]),
                    "created_at": h["created_at"],
                }
                for h in hyp_rows
            ]
        return {
            "id": fam.id,
            "idea": fam.idea,
            "budget": budget,
            "trial_count": trial_count,
            "hypotheses": hypotheses,
        }

    def run(self, idea: str, spec: dict[str, Any]) -> dict[str, Any]:
        self.run_calls.append((idea, spec))
        frozen = validate(spec)
        family = self.ledger.family_for(frozen.entry.block, frozen.symbol)
        if family is None:
            family = self.ledger.create_family(idea or frozen.name)
        hypothesis = self.ledger.register_hypothesis(family, frozen)

        verdict = self.verdicts_to_return.pop(0) if self.verdicts_to_return else "REJECT"
        closest_miss = json.dumps({"check": "profit_factor"}) if verdict == "REJECT" else None

        # Record a trial in SQLite ledger to track trial counts
        with self.ledger._write() as db:
            db.execute(
                "INSERT INTO trials (hypothesis_id, verdict, closest_miss_json, cost_json, "
                "bars_count, bars_first, bars_last, bars_sha256, rules_version, result_json) "
                "VALUES (?, ?, ?, '{}', 100, 1, 2, 'sha', 1, '{}')",
                (hypothesis, verdict, closest_miss),
            )

        return {
            "trial_id": len(self.run_calls),
            "hypothesis_id": hypothesis,
            "family_id": family,
            "verdict": verdict,
            "spec": spec,
        }


def _make_spec(symbol: str = "GOLD", period: int = 20) -> dict[str, Any]:
    spec = dict(EXAMPLE)
    spec["symbol"] = symbol
    spec["entry"] = {"block": "donchian_breakout", "params": {"period": period}}
    return spec


def test_nightshift_config_and_state_defaults():
    cfg = NightShiftConfig()
    assert cfg.max_trials == 20
    assert cfg.token_share == 0.50
    assert cfg.morning_brief_time == "07:00"

    state = NightShiftState()
    assert state.trials_run == 0
    assert state.candidates_found == 0
    assert state.tokens_used == 0
    assert state.stopped_reason == "pending"


def test_run_night_shift_no_desk():
    state = run_night_shift(desk=None, budget=None)
    assert isinstance(state, NightShiftState)
    assert state.stopped_reason == "completed"
    assert state.trials_run == 0
    assert state.tokens_used == 0


def test_run_night_shift_hit_trial_cap(tmp_path: Path):
    desk = FakeDesk(tmp_path)
    budget = TokenBudget(daily_cap=100_000)
    config = NightShiftConfig(max_trials=3, token_share=0.50)

    queued = [_make_spec(period=p) for p in (10, 15, 20, 25, 30)]
    state = run_night_shift(desk=desk, budget=budget, config=config, queued_ideas=queued)

    assert state.trials_run == 3
    assert state.stopped_reason == "hit_trial_cap_3"
    assert len(desk.run_calls) == 3


def test_run_night_shift_hit_token_budget_with_scripted_llm(tmp_path: Path):
    desk = FakeDesk(tmp_path)
    budget = TokenBudget(daily_cap=1_000)
    config = NightShiftConfig(max_trials=20, token_share=0.50)  # 500 token limit

    llm = ScriptedLLM(
        Turn(
            content=f"```json\n{json.dumps(_make_spec(period=15))}\n```",
            usage={"total_tokens": 300},
        ),
        Turn(
            content=f"```json\n{json.dumps(_make_spec(period=25))}\n```",
            usage={"total_tokens": 300},
        ),
        Turn(
            content=f"```json\n{json.dumps(_make_spec(period=35))}\n```",
            usage={"total_tokens": 300},
        ),
    )

    state = run_night_shift(
        desk=desk,
        budget=budget,
        config=config,
        llm=llm,
        queued_ideas=["idea 1", "idea 2", "idea 3"],
    )

    assert state.trials_run == 2
    assert state.tokens_used == 600
    assert budget.used == 600
    assert state.stopped_reason == "hit_token_budget_50%"


def test_run_night_shift_budget_paused_immediately(tmp_path: Path):
    desk = FakeDesk(tmp_path)
    budget = TokenBudget(daily_cap=1_000)
    budget.record_usage(1_000)
    assert budget.is_paused is True

    state = run_night_shift(desk=desk, budget=budget)
    assert state.trials_run == 0
    assert state.stopped_reason == "hit_token_budget_50%"


def test_run_night_shift_zero_max_trials(tmp_path: Path):
    desk = FakeDesk(tmp_path)
    budget = TokenBudget(daily_cap=100_000)
    config = NightShiftConfig(max_trials=0)

    state = run_night_shift(desk=desk, budget=budget, config=config)
    assert state.trials_run == 0
    assert state.stopped_reason == "hit_trial_cap_0"


def test_run_night_shift_immutable_sandbox_never_spends_holdout(tmp_path: Path):
    desk = FakeDesk(tmp_path)
    budget = TokenBudget(daily_cap=100_000)
    desk.verdicts_to_return = ["CANDIDATE", "REJECT"]

    queued = [_make_spec(period=20), _make_spec(period=30)]
    state = run_night_shift(desk=desk, budget=budget, queued_ideas=queued)

    assert state.trials_run == 2
    assert state.candidates_found == 1
    # Verify no holdout requests were filed
    assert len(desk.ledger.holdout_requests()) == 0


def test_run_night_shift_family_budget_exhausted_skipped(tmp_path: Path):
    desk = FakeDesk(tmp_path)
    budget = TokenBudget(daily_cap=100_000)

    # Pre-create family and fill budget
    fam_id = desk.ledger.create_family("Pre-existing family")
    hyp_id = desk.ledger.register_hypothesis(fam_id, _make_spec(period=20))
    current_budget = desk.ledger.budget(fam_id)  # 25
    # Add trials so budget is exhausted (25 used / 25 granted)
    with desk.ledger._write() as db:
        for _ in range(current_budget):
            db.execute(
                "INSERT INTO trials (hypothesis_id, verdict, closest_miss_json, cost_json, "
                "bars_count, bars_first, bars_last, bars_sha256, rules_version, result_json) "
                "VALUES (?, 'REJECT', '{\"check\": \"profit_factor\"}', '{}', 100, 1, 2, "
                "'sha', 1, '{}')",
                (hyp_id,),
            )
    assert desk.ledger.trial_count(fam_id) == current_budget
    assert desk.ledger.budget(fam_id) == current_budget

    # Queue an idea matching the same symbol and entry block
    state = run_night_shift(desk=desk, budget=budget, queued_ideas=[_make_spec(period=20)])
    # The family for donchian_breakout on GOLD has budget exhausted (25/25) -> skipped
    assert state.trials_run == 0
    assert state.stopped_reason == "completed"


def test_format_and_compile_morning_brief_with_recording_notifier():
    notifier = RecordingNotifier()
    state = NightShiftState(
        trials_run=18,
        candidates_found=3,
        tokens_used=24_500,
        stopped_reason="hit_trial_cap_20",
    )

    text = format_morning_brief(state)
    assert "Vibe Desk Night Shift Brief (07:00 Bangkok)" in text
    assert "Trials run: 18" in text
    assert "CANDIDATEs found: 3" in text
    assert "Tokens used: 24500" in text
    assert "Stop reason: hit_trial_cap_20" in text

    notice = compile_morning_brief(state, notifier=notifier)
    assert notice.kind == NoticeKind.DAILY_DIGEST
    assert notice.data["trials_run"] == 18
    assert notice.data["candidates_found"] == 3
    assert notice.data["tokens_used"] == 24_500
    assert notice.data["stopped_reason"] == "hit_trial_cap_20"

    assert len(notifier.notices) == 1
    assert notifier.last is notice


def test_run_night_shift_dispatches_morning_brief_if_notifier_passed(tmp_path: Path):
    desk = FakeDesk(tmp_path)
    budget = TokenBudget(daily_cap=100_000)
    notifier = RecordingNotifier()

    run_night_shift(
        desk=desk,
        budget=budget,
        queued_ideas=[_make_spec(period=20)],
        notifier=notifier,
    )

    assert len(notifier.notices) == 1
    assert notifier.last.kind == NoticeKind.DAILY_DIGEST
    assert notifier.last.data["trials_run"] == 1


def test_run_night_shift_file_queue(tmp_path: Path):
    desk = FakeDesk(tmp_path)
    budget = TokenBudget(daily_cap=100_000)

    # Write ideas to file
    q_file = tmp_path / "nightshift_queue.json"
    q_file.write_text(json.dumps([_make_spec(period=20), _make_spec(period=30)]))

    state = run_night_shift(desk=desk, budget=budget)
    assert state.trials_run == 2
    assert state.stopped_reason == "completed"


def test_generate_family_variations():
    base = _make_spec(period=20)
    variations = generate_family_variations(base, max_variations=4)
    assert len(variations) >= 1
    for var in variations:
        assert var["symbol"] == base["symbol"]
        assert var["entry"]["block"] == base["entry"]["block"]
        validated = validate(var)
        assert validated.symbol == "GOLD"
        assert validated.entry.block == "donchian_breakout"


def test_run_night_shift_family_variations_phase(tmp_path: Path):
    desk = FakeDesk(tmp_path)
    budget = TokenBudget(daily_cap=100_000)

    # First run a trial to create a family with 1 hypothesis and 1 trial
    run_night_shift(desk=desk, budget=budget, queued_ideas=[_make_spec(period=20)])
    assert len(desk.list_families()) == 1

    # Next night shift run has no queued ideas, but family exists and has trials from before
    state = run_night_shift(
        desk=desk,
        budget=budget,
        config=NightShiftConfig(max_trials=3),
    )
    assert state.trials_run == 3
    assert state.stopped_reason == "hit_trial_cap_3"


def test_run_night_shift_error_handling(tmp_path: Path):
    desk = FakeDesk(tmp_path)

    def faulty_run(idea: str, spec: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("Simulated test error in run")

    desk.run = faulty_run
    state = run_night_shift(desk=desk, queued_ideas=[_make_spec()])
    assert state.stopped_reason == "error"
