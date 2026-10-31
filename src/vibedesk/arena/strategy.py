"""Arena strategy model and lifecycle definitions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from vibedesk.arena.account import PaperAccount
from vibedesk.blocks.spec import StrategySpec


class StrategyStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    REMOVED = "removed"


class TwinType(StrEnum):
    STANDARD = "standard"
    REGIME_GATED = "regime_gated"


@dataclass
class ArenaStepResult:
    """Outcome of stepping an Arena strategy on one closed bar."""

    strategy_id: int
    bar_time: int
    signals_evaluated: int
    trades_opened: int
    trades_closed: int
    backtest_net_r: float
    live_net_r: float


class ArenaStrategy:
    """A CANDIDATE strategy executing on live closed MT5 bars in the Forward Arena."""

    def __init__(
        self,
        strategy_id: int,
        hypothesis_id: int,
        spec: StrategySpec,
        symbol: str,
        timeframe: str,
        twin_type: TwinType = TwinType.STANDARD,
        status: StrategyStatus = StrategyStatus.ACTIVE,
        initial_balance: float = 10_000.0,
        created_at: str = "",
    ) -> None:
        self.id = strategy_id
        self.hypothesis_id = hypothesis_id
        self.spec = spec
        self.symbol = symbol
        self.timeframe = timeframe
        self.twin_type = twin_type
        self.status = status
        self.initial_balance = initial_balance
        self.created_at = created_at

        # Two fill tracks running in parallel
        self.account_backtest = PaperAccount(initial_balance=initial_balance)
        self.account_live = PaperAccount(initial_balance=initial_balance)

    def step(self, bar: dict[str, Any], live_spread: float | None = None) -> ArenaStepResult:
        """Evaluate strategy on newly closed bar t and execute fills at bar t+1.

        Must re-check time filter against bar t+1 open (fill-bar rule, CCC-71).
        Teams implement full execution in Wave 1 (CCC-66).
        """
        if self.status != StrategyStatus.ACTIVE:
            return ArenaStepResult(
                strategy_id=self.id,
                bar_time=int(bar.get("time", 0)),
                signals_evaluated=0,
                trades_opened=0,
                trades_closed=0,
                backtest_net_r=self.account_backtest.net_r,
                live_net_r=self.account_live.net_r,
            )
        raise NotImplementedError("ArenaStrategy.step execution owned by Wave 1 (CCC-66)")

    def catch_up(self, bars: list[dict[str, Any]]) -> int:
        """Replay missed closed bars in sequence upon host restart or reconnection.

        Returns the number of replayed bars processed.
        Teams implement full catch-up in Wave 1 (CCC-66).
        """
        raise NotImplementedError("ArenaStrategy.catch_up replay owned by Wave 1 (CCC-66)")
