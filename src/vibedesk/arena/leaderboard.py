"""Forward Arena leaderboard calculation and ranking."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from vibedesk.arena.strategy import ArenaStrategy, TwinType


@dataclass(frozen=True)
class LeaderboardEntry:
    """A ranked strategy entry on the Arena leaderboard."""

    rank: int
    strategy_id: int
    hypothesis_id: int
    symbol: str
    timeframe: str
    net_r: float
    profit_factor: float
    max_drawdown: float
    trades_count: int
    win_rate: float
    decay_flag: bool
    demo_ready: bool
    twin_type: str


def compute_leaderboard(
    strategies: Sequence[ArenaStrategy],
    min_trades: int = 10,
) -> list[LeaderboardEntry]:
    """Rank strategies by forward net R, filtering for minimum trade count threshold.

    Teams implement full decay calculation against Gauntlet folds in Wave 2 (CCC-67).
    """
    eligible: list[ArenaStrategy] = [
        s for s in strategies if s.account_backtest.trades_count >= min_trades
    ]
    # Sort descending by net R
    sorted_strats = sorted(eligible, key=lambda s: s.account_backtest.net_r, reverse=True)

    entries: list[LeaderboardEntry] = []
    for rank, s in enumerate(sorted_strats, start=1):
        acct = s.account_backtest
        entries.append(
            LeaderboardEntry(
                rank=rank,
                strategy_id=s.id,
                hypothesis_id=s.hypothesis_id,
                symbol=s.symbol,
                timeframe=s.timeframe,
                net_r=round(acct.net_r, 2),
                profit_factor=round(acct.profit_factor, 2),
                max_drawdown=round(acct.max_drawdown, 4),
                trades_count=acct.trades_count,
                win_rate=round(acct.win_rate, 4),
                decay_flag=False,  # Computed in Wave 2
                demo_ready=False,  # Evaluated by check_demo_ready in Wave 2
                twin_type=s.twin_type.value
                if isinstance(s.twin_type, TwinType)
                else str(s.twin_type),
            )
        )
    return entries
