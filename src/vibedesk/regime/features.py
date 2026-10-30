"""Regime feature computation for Regime Desk."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RegimeFeatures:
    """Computed daily market features used for regime determination."""

    symbol: str
    timeframe: str
    atr_14: float
    atr_100_median: float
    volatility_ratio: float
    adx_14: float
    ma_50_slope: float
    calculated_at: str


def compute_daily_features(bars: Any, symbol: str) -> RegimeFeatures:
    """Compute volatility and trend metrics across recent daily and intraday bars.

    Teams implement full mathematical indicators in Wave 2 (CCC-76).
    """
    raise NotImplementedError("compute_daily_features owned by Wave 2 (CCC-76)")
