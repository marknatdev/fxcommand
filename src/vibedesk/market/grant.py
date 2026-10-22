"""The Holdout grant contract shared by the market facade and the Ledger (ADR 0002).

The market facade refuses any bars on or after HOLDOUT_START unless a GrantVerifier (the Ledger)
approves the grant it is shown. Only the Ledger issues grants, when the owner approves a
Holdout Request, and each Idea Family can spend one only once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

HOLDOUT_START = datetime(2025, 1, 1, tzinfo=UTC)
RESEARCH_START = datetime(2014, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class HoldoutGrant:
    request_id: int
    family_id: int
    token: str  # random secret the Ledger stored with the approval


class GrantVerifier(Protocol):
    def verify(self, grant: HoldoutGrant, symbol: str) -> bool:
        """True only for an approved, unspent grant whose Hypothesis trades `symbol`."""
        ...
