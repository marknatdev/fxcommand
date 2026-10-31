"""Night Shift autonomous overnight research runner (spec §6, ADR 0011, CCC-75)."""

from __future__ import annotations

import contextlib
import copy
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from vibedesk.app.llm import Turn
from vibedesk.app.research import DraftFailed, draft_spec, normalize
from vibedesk.blocks.spec import validate

logger = logging.getLogger("vibedesk.nightshift")


@dataclass(frozen=True)
class NightShiftConfig:
    """Configuration limits for Night Shift (ADR 0011)."""

    max_trials: int = 20
    token_share: float = 0.50
    morning_brief_time: str = "07:00"


@dataclass
class NightShiftState:
    """Execution status and summary for Night Shift."""

    trials_run: int = 0
    candidates_found: int = 0
    tokens_used: int = 0
    stopped_reason: str = "pending"
    started_at: str = ""
    finished_at: str = ""


class TokenTrackingLLM:
    """Wraps an LLM client to record token consumption into TokenBudget and NightShiftState."""

    def __init__(self, inner: Any, on_usage: Callable[[int | dict[str, Any]], None]) -> None:
        self.inner = inner
        self.on_usage = on_usage

    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> Turn:
        if hasattr(self.inner, "complete"):
            turn = self.inner.complete(messages, tools=tools)
            if turn and getattr(turn, "usage", None):
                self.on_usage(turn.usage)
            return turn
        content = self.inner.chat(messages)
        return Turn(content=content)

    def chat(self, messages: list[dict]) -> str:
        if hasattr(self.inner, "complete"):
            turn = self.complete(messages)
            return turn.content
        return str(self.inner.chat(messages))


def generate_family_variations(
    spec: dict[str, Any], max_variations: int = 4
) -> list[dict[str, Any]]:
    """Generate small parameter mutations of an existing StrategySpec.

    Guarantees:
    - Same symbol and entry block (belongs to the exact same Idea Family).
    - Valid StrategySpec adhering to schema and combinations <= 12.
    """
    variations: list[dict[str, Any]] = []
    base_name = spec.get("name", "Strategy")

    # 1. Vary exit stop multiplier
    stop_mult = spec.get("exit", {}).get("stop", {}).get("params", {}).get("mult", 2.0)
    for delta in (-0.5, 0.5, 1.0):
        new_mult = round(float(stop_mult) + delta, 1)
        if 1.0 <= new_mult <= 5.0 and new_mult != stop_mult:
            var = copy.deepcopy(spec)
            var["name"] = f"{base_name} (stop {new_mult}x)"[:80]
            var["exit"]["stop"]["params"]["mult"] = new_mult
            try:
                validate(normalize(var))
                variations.append(var)
            except Exception:
                pass
            if len(variations) >= max_variations:
                return variations

    # 2. Vary target R if target exists, or add one if absent
    target = spec.get("exit", {}).get("target")
    if target and target.get("block") == "r_target":
        r_val = target.get("params", {}).get("r", 2.0)
        curr_r = r_val[0] if isinstance(r_val, list) else r_val
        for delta in (-0.5, 0.5, 1.0):
            new_r = round(float(curr_r) + delta, 1)
            if 1.0 <= new_r <= 5.0 and new_r != curr_r:
                var = copy.deepcopy(spec)
                var["name"] = f"{base_name} (target {new_r}R)"[:80]
                var["exit"]["target"]["params"]["r"] = new_r
                try:
                    validate(normalize(var))
                    variations.append(var)
                except Exception:
                    pass
                if len(variations) >= max_variations:
                    return variations
    elif not target:
        for r_cand in (2.0, 3.0):
            var = copy.deepcopy(spec)
            var["name"] = f"{base_name} ({r_cand}R target)"[:80]
            var.setdefault("exit", {})["target"] = {
                "block": "r_target",
                "params": {"r": r_cand},
            }
            try:
                validate(normalize(var))
                variations.append(var)
            except Exception:
                pass
            if len(variations) >= max_variations:
                return variations

    # 3. Vary entry period if period param is present
    entry_params = spec.get("entry", {}).get("params", {})
    if "period" in entry_params:
        curr_p = entry_params["period"]
        p_val = curr_p[0] if isinstance(curr_p, list) else curr_p
        for delta in (-5, 5, 10):
            new_p = int(p_val + delta)
            if 10 <= new_p <= 100 and new_p != p_val:
                var = copy.deepcopy(spec)
                var["name"] = f"{base_name} (period {new_p})"[:80]
                var["entry"]["params"]["period"] = new_p
                try:
                    validate(normalize(var))
                    variations.append(var)
                except Exception:
                    pass
                if len(variations) >= max_variations:
                    return variations

    return variations


def run_night_shift(
    desk: Any = None,
    budget: Any = None,
    config: NightShiftConfig | None = None,
    llm: Any = None,
    queued_ideas: Sequence[str | dict[str, Any]] | None = None,
    notifier: Any = None,
    **kwargs: Any,
) -> NightShiftState:
    """Run autonomous overnight loop exploring queued ideas and family variations.

    Limits: capped at config.max_trials (default 20) and config.token_share * daily_cap.
    May run Trials only (never spends Holdout; never sends to Arena).
    """
    started_at = datetime.now(UTC).isoformat()
    state = NightShiftState(started_at=started_at)

    if desk is None:
        state.stopped_reason = "completed"
        state.finished_at = datetime.now(UTC).isoformat()
        return state

    # Resolve config
    if config is None:
        cap = 20
        share = 0.50
        if hasattr(desk, "root") and (desk.root / "settings.json").exists():
            try:
                from vibedesk.app.settings import load as load_settings

                s = load_settings(desk.root)
                cap = s.nightshift_trial_cap
                share = s.nightshift_token_share
            except Exception:
                pass
        config = NightShiftConfig(max_trials=cap, token_share=share)

    # Initial check on trial cap
    if config.max_trials <= 0:
        state.stopped_reason = f"hit_trial_cap_{config.max_trials}"
        state.finished_at = datetime.now(UTC).isoformat()
        if notifier is not None:
            from vibedesk.nightshift.brief import compile_morning_brief

            compile_morning_brief(state, notifier=notifier)
        return state

    # Initial check on token cap
    daily_cap = getattr(budget, "daily_cap", 100_000) if budget is not None else 100_000
    token_limit = int(daily_cap * config.token_share)
    if budget is not None and getattr(budget, "is_paused", False):
        state.stopped_reason = f"hit_token_budget_{int(config.token_share * 100)}%"
        state.finished_at = datetime.now(UTC).isoformat()
        if notifier is not None:
            from vibedesk.nightshift.brief import compile_morning_brief

            compile_morning_brief(state, notifier=notifier)
        return state

    if config.token_share <= 0:
        state.stopped_reason = "hit_token_budget_0%"
        state.finished_at = datetime.now(UTC).isoformat()
        if notifier is not None:
            from vibedesk.nightshift.brief import compile_morning_brief

            compile_morning_brief(state, notifier=notifier)
        return state

    def on_token_usage(usage: int | dict[str, Any]) -> None:
        tokens = usage if isinstance(usage, int) else int(usage.get("total_tokens", 0))
        state.tokens_used += max(0, tokens)
        if budget is not None and hasattr(budget, "record_usage"):
            budget.record_usage(tokens)

    # Wrap LLM if provided
    active_llm = TokenTrackingLLM(llm, on_token_usage) if llm is not None else None

    # Track existing families before night shift starts for phase 2 variations
    existing_family_ids: set[int] = set()
    if hasattr(desk, "list_families"):
        with contextlib.suppress(Exception):
            existing_family_ids = {
                fam["id"]
                for fam in desk.list_families()
                if fam.get("trial_count", 0) > 0 and fam.get("id") is not None
            }

    # Collect work queue
    # 1. Owner's queued ideas
    ideas_to_run: list[str | dict[str, Any]] = list(queued_ideas or [])

    # 2. File-based queue
    if hasattr(desk, "root"):
        q_file = desk.root / "nightshift_queue.json"
        if q_file.exists():
            try:
                file_items = json.loads(q_file.read_text(encoding="utf-8"))
                if isinstance(file_items, list):
                    ideas_to_run.extend(file_items)
            except Exception:
                logger.warning("Failed to read nightshift_queue.json", exc_info=True)

    # 3. Untested families from Ledger
    if hasattr(desk, "list_families"):
        try:
            for fam in desk.list_families():
                if (
                    fam.get("trial_count", 0) == 0
                    and fam.get("idea")
                    and fam["idea"] not in ideas_to_run
                ):
                    ideas_to_run.append(fam["idea"])
        except Exception:
            logger.warning("Failed to list families from ledger", exc_info=True)

    def should_stop() -> str | None:
        if state.trials_run >= config.max_trials:
            return f"hit_trial_cap_{config.max_trials}"
        if state.tokens_used >= token_limit:
            return f"hit_token_budget_{int(config.token_share * 100)}%"
        if budget is not None and getattr(budget, "is_paused", False):
            return f"hit_token_budget_{int(config.token_share * 100)}%"
        return None

    try:
        # Phase 1: Run queued ideas
        for item in ideas_to_run:
            stop_reason = should_stop()
            if stop_reason:
                state.stopped_reason = stop_reason
                break

            idea_text = ""
            spec_dict = None
            if isinstance(item, dict):
                spec_dict = item
                idea_text = item.get("name", "Queued Strategy")
            elif isinstance(item, str):
                idea_text = item
                if active_llm is not None:
                    try:
                        draft = draft_spec(active_llm, idea_text)
                        spec_dict = draft.spec
                    except DraftFailed as e:
                        logger.warning("Failed to draft spec for idea %r: %s", idea_text, e)
                        continue
                    except Exception as e:
                        if "ScriptedLLM ran out" in str(e):
                            logger.info("LLM ran out of turns; stopping ideas phase")
                            break
                        raise
                else:
                    try:
                        spec_dict = json.loads(idea_text)
                    except Exception:
                        logger.warning("No LLM available to draft idea: %r", idea_text)
                        continue

            if spec_dict is not None:
                try:
                    frozen_candidate = validate(normalize(spec_dict))
                except Exception:
                    logger.warning("Invalid spec: %r", spec_dict, exc_info=True)
                    continue

                if hasattr(desk, "ledger") and hasattr(desk.ledger, "family_for"):
                    fam_id = desk.ledger.family_for(
                        frozen_candidate.entry.block, frozen_candidate.symbol
                    )
                    if fam_id is not None:
                        used = desk.ledger.trial_count(fam_id)
                        fam_budget = desk.ledger.budget(fam_id)
                        if (fam_budget > 0 and used >= fam_budget) or (
                            fam_budget == 0 and used > 0
                        ):
                            logger.info(
                                "Family %d budget full (%d/%d), skipping", fam_id, used, fam_budget
                            )
                            continue

                summary = desk.run(idea_text, spec_dict)
                state.trials_run += 1
                if summary.get("verdict") == "CANDIDATE":
                    state.candidates_found += 1

                stop_reason = should_stop()
                if stop_reason:
                    state.stopped_reason = stop_reason
                    break

        # Phase 2: Run small variations of existing families
        if (
            state.stopped_reason == "pending"
            and hasattr(desk, "list_families")
            and hasattr(desk, "get_family")
            and existing_family_ids
        ):
            for fam in desk.list_families():
                fam_id = fam.get("id")
                if fam_id not in existing_family_ids:
                    continue

                stop_reason = should_stop()
                if stop_reason:
                    state.stopped_reason = stop_reason
                    break

                used = desk.ledger.trial_count(fam_id)
                fam_budget = desk.ledger.budget(fam_id)
                if (fam_budget > 0 and used >= fam_budget) or (fam_budget == 0 and used > 0):
                    continue

                fam_detail = desk.get_family(fam_id)
                hypotheses = fam_detail.get("hypotheses", [])
                if not hypotheses:
                    continue

                base_spec = hypotheses[0].get("spec")
                if not base_spec:
                    continue

                variations: list[dict[str, Any]] = []
                if active_llm is not None:
                    try:
                        prompt = f"Create a variation of strategy for {fam.get('idea', 'strategy')}"
                        draft = draft_spec(active_llm, prompt)
                        variations.append(draft.spec)
                    except Exception:
                        pass

                remaining_budget = fam_budget - used if fam_budget > 0 else 4
                variations.extend(
                    generate_family_variations(base_spec, max_variations=remaining_budget)
                )

                for var_spec in variations:
                    stop_reason = should_stop()
                    if stop_reason:
                        state.stopped_reason = stop_reason
                        break

                    used = desk.ledger.trial_count(fam_id)
                    fam_budget = desk.ledger.budget(fam_id)
                    if (fam_budget > 0 and used >= fam_budget) or (fam_budget == 0 and used > 0):
                        break

                    summary = desk.run(fam.get("idea", "Family Variation"), var_spec)
                    state.trials_run += 1
                    if summary.get("verdict") == "CANDIDATE":
                        state.candidates_found += 1

                    stop_reason = should_stop()
                    if stop_reason:
                        state.stopped_reason = stop_reason
                        break

        if state.stopped_reason == "pending":
            state.stopped_reason = "completed"

    except Exception:
        logger.error("Night Shift encountered unhandled exception", exc_info=True)
        state.stopped_reason = "error"

    state.finished_at = datetime.now(UTC).isoformat()
    if notifier is not None:
        from vibedesk.nightshift.brief import compile_morning_brief

        compile_morning_brief(state, notifier=notifier)
    return state
