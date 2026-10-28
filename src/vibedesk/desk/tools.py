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

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vibedesk.app.research import normalize
from vibedesk.blocks.registry import LIBRARY
from vibedesk.blocks.spec import expand_grid, validate
from vibedesk.desk.loop import ToolRegistry
from vibedesk.market.bars import Timeframe
from vibedesk.market.universe import SYMBOLS


@dataclass
class DeskContext:
    """Dependencies and context needed by the Research Desk tools."""

    ledger: Any  # Ledger
    market: Any  # MarketData
    family_id: int
    budget: Any | None = None  # TokenBudget
    notifier: Any | None = None  # Notifier
    root: Path | None = None


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
        """Wave 1 Desk team will implement full stats query in CCC-44."""
        raise NotImplementedError("describe_data will be implemented in CCC-44")

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
        except Exception as error:
            return {
                "valid": False,
                "spec": spec,
                "combinations": 0,
                "errors": [str(error)],
            }

    @registry.register(
        "register_hypothesis",
        description="Freeze a Strategy Spec as a Hypothesis in the Ledger for the family.",
        parameters=REGISTER_HYPOTHESIS_SCHEMA,
    )
    def register_hypothesis(spec: dict[str, Any], family_id: int | None = None) -> dict[str, Any]:
        """Wave 1 Desk team will implement full Ledger registration in CCC-44."""
        raise NotImplementedError("register_hypothesis will be implemented in CCC-44")

    @registry.register(
        "run_trial",
        description="Run the Gauntlet for a registered Hypothesis. Charges 1 Trial to budget.",
        parameters=RUN_TRIAL_SCHEMA,
    )
    def run_trial(hypothesis_id: int) -> dict[str, Any]:
        """Wave 1 Desk team will implement full trial execution in CCC-44."""
        raise NotImplementedError("run_trial will be implemented in CCC-44")

    @registry.register(
        "get_trial",
        description="Return stored results and metrics for a completed Trial from the Ledger.",
        parameters=GET_TRIAL_SCHEMA,
    )
    def get_trial(trial_id: int) -> dict[str, Any]:
        """Wave 1 Desk team will implement full trial retrieval in CCC-44."""
        raise NotImplementedError("get_trial will be implemented in CCC-44")

    @registry.register(
        "request_holdout",
        description="Request spending the Idea Family's Holdout data for a CANDIDATE Hypothesis.",
        parameters=REQUEST_HOLDOUT_SCHEMA,
    )
    def request_holdout(hypothesis_id: int) -> dict[str, Any]:
        """Wave 1 Desk team will implement Holdout Request gate check in CCC-44."""
        raise NotImplementedError("request_holdout will be implemented in CCC-44")

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
