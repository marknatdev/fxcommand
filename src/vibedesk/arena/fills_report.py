"""Fill comparison report between Backtest-Cost Track and Live-Spread Track."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from vibedesk.arena.strategy import ArenaStrategy


@dataclass(frozen=True)
class FillComparisonRow:
    """Comparison metrics for a single trade evaluated across both tracks."""

    strategy_id: int
    trade_id: int
    symbol: str
    entry_time: int
    exit_time: int
    backtest_r: float
    live_spread_r: float
    r_divergence: float
    spread_delta: float


def generate_fills_report(
    strategies: Sequence[ArenaStrategy],
) -> list[FillComparisonRow]:
    """Generate divergence report between the backtest-cost and live-spread tracks (spec §5.2)."""
    rows: list[FillComparisonRow] = []
    for s in strategies:
        bt_trades = {t.id: t for t in s.account_backtest.closed_trades}
        live_trades = {t.id: t for t in s.account_live.closed_trades}

        common_ids = set(bt_trades.keys()) & set(live_trades.keys())
        for tid in sorted(common_ids):
            bt = bt_trades[tid]
            lv = live_trades[tid]
            rows.append(
                FillComparisonRow(
                    strategy_id=s.id,
                    trade_id=tid,
                    symbol=s.symbol,
                    entry_time=bt.entry_time,
                    exit_time=bt.exit_time,
                    backtest_r=round(bt.r, 3),
                    live_spread_r=round(lv.r, 3),
                    r_divergence=round(bt.r - lv.r, 3),
                    spread_delta=round(lv.cost_paid - bt.cost_paid, 5),
                )
            )
    return rows
