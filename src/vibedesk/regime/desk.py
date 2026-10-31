"""Regime Desk: daily typed LLM gating for Arena strategy twins."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from vibedesk.regime.features import RegimeFeatures


class RegimeReason(StrEnum):
    """Fixed menu of reasons for regime ON/OFF decisions (spec §7.2)."""

    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    STRONG_TREND = "STRONG_TREND"
    CHOPPY = "CHOPPY"
    RANGE_BOUND = "RANGE_BOUND"
    NORMAL = "NORMAL"


@dataclass(frozen=True)
class RegimeDecision:
    """A daily on/off toggle for an Arena strategy twin."""

    strategy_id: int
    active: bool
    reason: RegimeReason
    evaluated_at: str


def evaluate_regimes(
    features: list[RegimeFeatures],
    strategies: list[Any],
    llm: Any,
) -> list[RegimeDecision]:
    """Invoke typed LLM call to decide on/off status per Arena strategy twin.

    Teams implement structured prompt and parsing in Wave 2 (CCC-76).
    """
    raise NotImplementedError("evaluate_regimes prompt and parsing owned by Wave 2 (CCC-76)")
