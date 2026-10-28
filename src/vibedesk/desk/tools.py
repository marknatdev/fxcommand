"""The Research Desk tools (spec §7.2, CCC-44).

Tools exposed to the Research Desk LLM loop:
1. `list_blocks`: returns the Block Library.
2. `describe_data`: returns statistics on the Research Window only.
3. `draft_spec`: validates a Strategy Spec and returns parameter grid size (no Trial counted).
4. `register_hypothesis`: freezes a Hypothesis in the Ledger for the current Idea Family.
5. `run_trial`: runs the Gauntlet, charges 1 Trial against the budget. Refused if budget spent.
6. `get_trial`: returns stored results and metrics for a completed Trial.
7. `request_holdout`: requests spending Holdout data, through the gate in spec §6.3.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from vibedesk.app.research import normalize
from vibedesk.blocks.indicators import atr
from vibedesk.blocks.registry import LIBRARY
from vibedesk.blocks.spec import expand_grid, validate
from vibedesk.desk.loop import ToolRegistry
from vibedesk.engine.costs import CostSnapshot
from vibedesk.gauntlet.trial import (
    request_holdout as gauntlet_request_holdout,
)
from vibedesk.gauntlet.trial import (
    run_trial as gauntlet_run_trial,
)
from vibedesk.ledger import BudgetExhausted, NotFound
from vibedesk.market.bars import Timeframe
from vibedesk.market.costs import CostError, current_cost
from vibedesk.market.grant import HOLDOUT_START, RESEARCH_START
from vibedesk.market.universe import SYMBOLS
from vibedesk.notify import Notice, NoticeKind


@dataclass
class DeskContext:
    """Dependencies and context needed by the Research Desk tools."""

    ledger: Any  # Ledger
    market: Any  # MarketData
    family_id: int
    budget: Any | None = None  # TokenBudget
    notifier: Any | None = None  # Notifier
    root: Path | None = None
    cost: CostSnapshot | None = None  # Optional override cost snapshot
    use_m1: bool | None = None  # Optional override for M1 conflict resolution


# Tool parameter schemas per spec §7.2
LIST_BLOCKS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
}

DESCRIBE_DATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "symbol": {
            "type": "string",
            "enum": list(SYMBOLS),
            "description": "Symbol name from the universe.",
        },
        "timeframe": {
            "type": "string",
            "enum": [tf.value for tf in Timeframe if tf != Timeframe.M1],
            "description": "Timeframe (M15, H1, H4, D1).",
        },
    },
    "required": ["symbol", "timeframe"],
}

DRAFT_SPEC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "spec": {
            "type": "object",
            "description": "The complete Strategy Spec dictionary.",
        },
    },
    "required": ["spec"],
}

REGISTER_HYPOTHESIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "spec": {
            "type": "object",
            "description": "The validated Strategy Spec dictionary to freeze.",
        },
        "family_id": {
            "type": "integer",
            "description": "Optional Idea Family ID; defaults to current chat family.",
        },
    },
    "required": ["spec"],
}

RUN_TRIAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hypothesis_id": {
            "type": "integer",
            "description": "The frozen Hypothesis ID to run through the Gauntlet.",
        },
    },
    "required": ["hypothesis_id"],
}

GET_TRIAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "trial_id": {
            "type": "integer",
            "description": "The Trial ID to retrieve.",
        },
    },
    "required": ["trial_id"],
}

REQUEST_HOLDOUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hypothesis_id": {
            "type": "integer",
            "description": "The CANDIDATE Hypothesis ID requesting the Holdout.",
        },
    },
    "required": ["hypothesis_id"],
}


def build_desk_tools(ctx: DeskContext) -> ToolRegistry:
    """Build the ToolRegistry containing all 7 Research Desk tools (spec §7.2, CCC-44)."""
    registry = ToolRegistry()

    @registry.register(
        "list_blocks",
        description="Return the Block Library with all entries, filters, exits and sizing blocks.",
        parameters=LIST_BLOCKS_SCHEMA,
    )
    def list_blocks() -> dict[str, Any]:
        return {
            "blocks": [
                {
                    "name": b.name,
                    "kind": b.kind.value,
                    "description": b.__doc__ or "",
                }
                for b in LIBRARY.all()
            ]
        }

    @registry.register(
        "describe_data",
        description="Return statistics on the Research Window data for a symbol and timeframe.",
        parameters=DESCRIBE_DATA_SCHEMA,
    )
    def describe_data(symbol: str, timeframe: str) -> dict[str, Any]:
        if ctx.market is None:
            raise ValueError("market is not configured in DeskContext")
        if symbol not in SYMBOLS:
            raise ValueError(f"unknown symbol {symbol!r}; expected one of {SYMBOLS}")
        tf = Timeframe(timeframe)
        if tf == Timeframe.M1:
            raise ValueError("describe_data only queries trading timeframes (M15, H1, H4, D1)")

        # Query the Research Window only: RESEARCH_START to HOLDOUT_START (spec §7.2, §4)
        bars = ctx.market.bars(symbol, tf, RESEARCH_START, HOLDOUT_START)
        count = len(bars.time)
        start_date: str | None = None
        end_date: str | None = None
        atr_p50: float | None = None

        if count > 0:
            start_date = datetime.fromtimestamp(int(bars.time[0]), tz=UTC).date().isoformat()
            end_date = datetime.fromtimestamp(int(bars.time[-1]), tz=UTC).date().isoformat()
            if count >= 14:
                atr_series = atr(bars.high, bars.low, bars.close, 14)
                valid = atr_series[~np.isnan(atr_series)]
                if len(valid) > 0:
                    atr_p50 = round(float(np.median(valid)), bars.digits)

        floor: float | None = None
        if ctx.cost is not None:
            floor = ctx.cost.spread_floor_points
        elif ctx.root is not None:
            with contextlib.suppress(CostError):
                floor = current_cost(ctx.root, symbol).spread_floor_points

        return {
            "symbol": symbol,
            "timeframe": tf.value,
            "bar_count": count,
            "start_date": start_date,
            "end_date": end_date,
            "spread_floor": floor,
            "atr_p50": atr_p50,
        }

    @registry.register(
        "draft_spec",
        description="Validate a Strategy Spec and return errors or the parameter grid size.",
        parameters=DRAFT_SPEC_SCHEMA,
    )
    def draft_spec(spec: dict[str, Any]) -> dict[str, Any]:
        try:
            validated = validate(normalize(spec))
            combinations = len(expand_grid(validated))
            return {
                "valid": True,
                "spec": validated.model_dump(mode="json"),
                "combinations": combinations,
                "errors": [],
            }
        except ValueError as error:
            return {
                "valid": False,
                "spec": spec,
                "combinations": 0,
                "errors": [str(error)],
                "error": f"{error.__class__.__name__}: {error}",
            }

    @registry.register(
        "register_hypothesis",
        description="Freeze a Strategy Spec as a Hypothesis in the Ledger for the family.",
        parameters=REGISTER_HYPOTHESIS_SCHEMA,
    )
    def register_hypothesis(spec: dict[str, Any], family_id: int | None = None) -> dict[str, Any]:
        if ctx.ledger is None:
            raise ValueError("ledger is not configured in DeskContext")
        validated = validate(normalize(spec))

        # Anti-gaming rule (ADR 0003, spec §6.4): check if (entry_block, symbol)
        # already belongs to another Idea Family. If so, register there so counts cannot reset.
        existing_family = ctx.ledger.family_for(validated.entry.block, validated.symbol)
        target_family_id = family_id if family_id is not None else ctx.family_id
        if existing_family is not None:
            target_family_id = existing_family
        elif target_family_id is None or target_family_id == 0:
            target_family_id = ctx.ledger.create_family(validated.name)
            ctx.family_id = target_family_id

        hyp_id = ctx.ledger.register_hypothesis(target_family_id, validated.model_dump(mode="json"))
        return {
            "hypothesis_id": hyp_id,
            "family_id": target_family_id,
            "entry_block": validated.entry.block,
            "symbol": validated.symbol,
            "timeframe": validated.timeframe,
            "combinations": len(expand_grid(validated)),
        }

    @registry.register(
        "run_trial",
        description="Run the Gauntlet for a registered Hypothesis. Charges 1 Trial to budget.",
        parameters=RUN_TRIAL_SCHEMA,
    )
    def run_trial(hypothesis_id: int) -> dict[str, Any]:
        if ctx.ledger is None:
            raise ValueError("ledger is not configured in DeskContext")
        if ctx.market is None:
            raise ValueError("market is not configured in DeskContext")

        hyp = ctx.ledger.hypothesis(hypothesis_id)
        symbol = hyp.symbol

        # Spec §6.4, §7.2: charge 1 Trial against family budget. Refused once budget is spent.
        # The Research Desk cannot extend a budget.
        used = ctx.ledger.trial_count(hyp.family_id)
        budget = ctx.ledger.budget(hyp.family_id)
        if used >= budget:
            raise BudgetExhausted(hyp.family_id, used, budget)

        # Resolve cost snapshot
        if ctx.cost is not None:
            cost = ctx.cost
        elif ctx.root is not None:
            try:
                cost = current_cost(ctx.root, symbol)
            except CostError:
                point = 0.01 if symbol == "GOLD" else 0.00001
                cost = CostSnapshot(point=point, spread_floor_points=0.0, slippage_points=1.0)
        else:
            point = 0.01 if symbol == "GOLD" else 0.00001
            cost = CostSnapshot(point=point, spread_floor_points=0.0, slippage_points=1.0)

        # Determine M1 usage
        use_m1 = True
        if ctx.use_m1 is not None:
            use_m1 = ctx.use_m1
        elif ctx.root is not None:
            from vibedesk.market.cache import first_cached

            use_m1 = first_cached(ctx.root, symbol, Timeframe.M1) is not None

        trial_res = gauntlet_run_trial(
            ctx.ledger,
            ctx.market,
            hypothesis_id,
            cost,
            use_m1=use_m1,
        )

        # Send CANDIDATE notice via notifier if present (spec §8.4)
        if trial_res.verdict.verdict == "CANDIDATE" and ctx.notifier is not None:
            ctx.notifier.send(
                Notice(
                    kind=NoticeKind.CANDIDATE,
                    text=f"Hypothesis {hypothesis_id} ({symbol} {hyp.timeframe}) reached CANDIDATE",
                    data={
                        "trial_id": trial_res.trial_id,
                        "hypothesis_id": hypothesis_id,
                        "symbol": symbol,
                        "timeframe": hyp.timeframe,
                    },
                )
            )

        return {
            "trial_id": trial_res.trial_id,
            "hypothesis_id": hypothesis_id,
            "verdict": trial_res.verdict.verdict,
            "closest_miss": trial_res.verdict.closest_miss,
            "record": trial_res.record,
        }

    @registry.register(
        "get_trial",
        description="Return stored results and metrics for a completed Trial from the Ledger.",
        parameters=GET_TRIAL_SCHEMA,
    )
    def get_trial(trial_id: int) -> dict[str, Any]:
        if ctx.ledger is None:
            raise ValueError("ledger is not configured in DeskContext")
        try:
            tr = ctx.ledger.trial(trial_id)
        except NotFound:
            raise
        result_record = json.loads(tr.result_json)
        return {
            "trial_id": tr.id,
            "hypothesis_id": tr.hypothesis_id,
            "verdict": tr.verdict,
            "closest_miss": tr.closest_miss,
            "cost": tr.cost.to_json(),
            "created_at": tr.created_at,
            "rules_version": tr.rules_version,
            "result": result_record,
        }

    @registry.register(
        "request_holdout",
        description="Request spending the Idea Family's Holdout data for a CANDIDATE Hypothesis.",
        parameters=REQUEST_HOLDOUT_SCHEMA,
    )
    def request_holdout(hypothesis_id: int) -> dict[str, Any]:
        if ctx.ledger is None:
            raise ValueError("ledger is not configured in DeskContext")

        # Run gate checks via gauntlet.trial.request_holdout (spec §6.3):
        # verifies CANDIDATE, rules_version, Deflated Sharpe, expected trades >= 30.
        req_id = gauntlet_request_holdout(ctx.ledger, hypothesis_id)

        # Notify owner of pending Holdout Request (spec §8.4)
        if ctx.notifier is not None:
            ctx.notifier.send(
                Notice(
                    kind=NoticeKind.HOLDOUT_REQUEST,
                    text=(
                        f"Holdout Request {req_id} for Hypothesis {hypothesis_id} "
                        "awaiting owner approval"
                    ),
                    data={"request_id": req_id, "hypothesis_id": hypothesis_id},
                )
            )

        return {
            "request_id": req_id,
            "hypothesis_id": hypothesis_id,
            "status": "awaiting_owner",
        }

    # Attach context to registry for easy retrieval in desk_loop
    registry.ctx = ctx  # type: ignore[attr-defined]
    return registry


__all__ = [
    "DESCRIBE_DATA_SCHEMA",
    "DRAFT_SPEC_SCHEMA",
    "GET_TRIAL_SCHEMA",
    "LIST_BLOCKS_SCHEMA",
    "REGISTER_HYPOTHESIS_SCHEMA",
    "REQUEST_HOLDOUT_SCHEMA",
    "RUN_TRIAL_SCHEMA",
    "DeskContext",
    "build_desk_tools",
]
