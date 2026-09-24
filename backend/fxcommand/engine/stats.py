"""Performance statistics over closed trades."""

from __future__ import annotations

from typing import Iterable

from ..store import TradeRow


def trade_stats(trades: Iterable[TradeRow]) -> dict:
    closed = sorted((t for t in trades if t.status == "closed" and t.profit is not None), key=lambda t: t.close_time or 0)
    profits = [float(t.profit) for t in closed]
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    peak = equity = max_dd = 0.0
    for p in profits:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    n = len(profits)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
        "net_profit": round(sum(profits), 2),
        "gross_profit": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,  # None = no losing trades yet
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "expectancy": round(sum(profits) / n, 2) if n else 0.0,
        "best": round(max(profits), 2) if profits else 0.0,
        "worst": round(min(profits), 2) if profits else 0.0,
        "max_drawdown": round(max_dd, 2),
    }

