"""Owner controls for Forward Arena strategies."""

from __future__ import annotations

from typing import Any

from vibedesk.arena.strategy import StrategyStatus


class ArenaControls:
    """Owner controls interface for modifying Arena strategy state.

    All actions are audited in the Ledger table arena_actions.
    """

    def __init__(self, ledger: Any = None, runtime: Any = None) -> None:
        self.ledger = ledger
        self.runtime = runtime
        self.actions: list[dict[str, Any]] = []

    def pause_strategy(self, strategy_id: int, reason: str = "") -> None:
        """Pause execution of a strategy. Open positions manage to completion."""
        if self.runtime is not None and hasattr(self.runtime, "strategies"):
            strat = self.runtime.strategies.get(strategy_id)
            if strat is not None:
                strat.status = StrategyStatus.PAUSED
        self._record_action(strategy_id=strategy_id, action="paused", reason=reason)

    def resume_strategy(self, strategy_id: int, reason: str = "") -> None:
        """Resume a paused strategy."""
        if self.runtime is not None and hasattr(self.runtime, "strategies"):
            strat = self.runtime.strategies.get(strategy_id)
            if strat is not None:
                strat.status = StrategyStatus.ACTIVE
        self._record_action(strategy_id=strategy_id, action="resumed", reason=reason)

    def remove_strategy(self, strategy_id: int, reason: str = "") -> None:
        """Permanently remove a strategy from the Arena."""
        if self.runtime is not None and hasattr(self.runtime, "strategies"):
            strat = self.runtime.strategies.get(strategy_id)
            if strat is not None:
                strat.status = StrategyStatus.REMOVED
        self._record_action(strategy_id=strategy_id, action="removed", reason=reason)

    def stop_arena(self, reason: str = "emergency_stop") -> None:
        """Global emergency pause across all Arena strategies."""
        if self.runtime is not None:
            if hasattr(self.runtime, "stop"):
                self.runtime.stop()
            elif hasattr(self.runtime, "strategies"):
                self.runtime._stopped = True
                for s in self.runtime.strategies.values():
                    s.status = StrategyStatus.PAUSED
        self._record_action(strategy_id=None, action="stopped", reason=reason)

    def _record_action(self, strategy_id: int | None, action: str, reason: str) -> None:
        record = {"strategy_id": strategy_id, "action": action, "reason": reason}
        self.actions.append(record)
        if self.ledger is not None and hasattr(self.ledger, "record_arena_action"):
            self.ledger.record_arena_action(strategy_id=strategy_id, action=action, reason=reason)
        elif self.ledger is not None and hasattr(self.ledger, "_db"):
            try:
                with self.ledger._lock:
                    self.ledger._db.execute(
                        "INSERT INTO arena_actions (strategy_id, action, reason) VALUES (?, ?, ?)",
                        (strategy_id, action, reason),
                    )
                    self.ledger._db.commit()
            except Exception:
                pass
