"""Inputs accepted by the SessionManager (also the API request bodies)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..broker.types import Timeframe


class DomainError(Exception):
    """A command violated a domain rule. ``code`` is stable for clients; ``status`` is the HTTP mapping."""

    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class AssignmentIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=40)
    timeframe: Timeframe = Timeframe.M15
    strategy: str = "ema_cross"
    params: dict[str, Any] = Field(default_factory=dict)
    risk_profile_id: int | None = None
    reverse_on_opposite: bool = True
    enabled: bool = True

    @field_validator("symbol")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class SessionIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    notes: str = ""
    auto_resume: bool = False
    max_positions: int = Field(5, ge=1, le=100)
    daily_loss_pct: float = Field(3.0, gt=0, le=100)
    window: dict[str, Any] | None = None
    assignments: list[AssignmentIn] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v
