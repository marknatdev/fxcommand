"""Sizing Blocks. Results are measured in R, so sizing only shapes the money view of a Trial."""

from pydantic import BaseModel, ConfigDict, Field

from vibedesk.blocks.registry import LIBRARY, Kind


class FixedRiskParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Capped at the owner's 1%: a Strategy Spec may risk less, never more.
    risk_pct: float = Field(1.0, gt=0, le=1.0)


@LIBRARY.register("fixed_risk", Kind.SIZING, FixedRiskParams)
def fixed_risk(p: FixedRiskParams) -> float:
    """Risk a fixed percentage of the balance on every trade."""
    return p.risk_pct / 100
