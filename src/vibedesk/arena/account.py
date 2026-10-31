"""Virtual Paper Account for Forward Arena strategies."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Position:
    """An open virtual position in the Arena."""

    symbol: str
    direction: int  # 1 for long, -1 for short
    entry_time: int
    entry_price: float
    size: float
    stop_price: float
    target_price: float | None = None
    stop_distance: float = 0.0
    trail_price: float | None = None
    entry_cost: float = 0.0
    bars_held: int = 0
    opposite_due: bool = False


@dataclass(frozen=True)
class PaperTrade:
    """A closed virtual trade in the Arena."""

    id: int
    symbol: str
    direction: int
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    size: float
    pnl: float
    r: float
    cost_paid: float
    exit_reason: str


@dataclass
class PaperAccount:
    """A virtual 10,000 USD account assigned to one strategy and fill track."""

    initial_balance: float = 10_000.0
    balance: float = 10_000.0
    equity: float = 10_000.0
    peak_equity: float = 10_000.0
    closed_trades: list[PaperTrade] = field(default_factory=list)
    open_position: Position | None = None

    @property
    def net_r(self) -> float:
        """Total closed trade return in units of R."""
        return sum(t.r for t in self.closed_trades)

    @property
    def trades_count(self) -> int:
        return len(self.closed_trades)

    @property
    def profit_factor(self) -> float:
        """Sum of winning trade PnL / sum of absolute losing trade PnL."""
        wins = sum(t.pnl for t in self.closed_trades if t.pnl > 0)
        losses = abs(sum(t.pnl for t in self.closed_trades if t.pnl < 0))
        if losses == 0.0:
            return float("inf") if wins > 0 else 0.0
        return wins / losses

    @property
    def max_drawdown(self) -> float:
        """Maximum peak-to-trough equity drawdown percentage."""
        if self.peak_equity <= 0:
            return 0.0
        dd = (self.peak_equity - self.equity) / self.peak_equity
        return max(0.0, float(dd))

    @property
    def win_rate(self) -> float:
        if not self.closed_trades:
            return 0.0
        wins = sum(1 for t in self.closed_trades if t.pnl > 0)
        return wins / len(self.closed_trades)
