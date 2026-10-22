"""A Trial's numbers: results in R, the daily R series and a money view of the equity curve.

R metrics are the ones the Pass Bar judges. The money view (risk a fixed fraction of a
balance per trade, compounding) is for the owner to read, never for judging.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from vibedesk.engine.simulate import Trades
from vibedesk.market.bars import Bars
from vibedesk.market.server_time import DAY, server_offset


@dataclass(frozen=True, slots=True, eq=False)
class Metrics:
    trades: int
    net_r: float
    profit_factor: float  # gross winning R / gross losing R; inf with no losers, NaN with no trades
    win_rate: float  # shown on the Verdict Card, never judged (ADR 0007)
    expectancy_r: float  # mean R per trade
    top5_share: float  # share of net R earned by the 5 best trades; NaN when net R <= 0
    max_drawdown_r: float  # deepest fall of cumulative R from a previous peak
    day_start: np.ndarray  # UTC epoch seconds of each trading day's server midnight
    daily_r: np.ndarray  # R of the trades that exited that day; 0 on days without exits
    equity: np.ndarray  # balance after each trade, starting balance first
    max_drawdown_money: float
    max_drawdown_pct: float


def metrics(
    trades: Trades, bars: Bars, risk_fraction: float = 0.01, balance: float = 1000.0
) -> Metrics:
    r = np.asarray(trades["r"], dtype=np.float64)
    n = len(r)
    net = r.sum()
    top5 = np.sort(r)[::-1][:5].sum() / net if net > 0 else np.nan

    cumulative = np.r_[0.0, np.cumsum(r)]
    max_drawdown_r = float((np.maximum.accumulate(cumulative) - cumulative).max())

    equity = balance * np.r_[1.0, np.cumprod(1 + risk_fraction * r)]
    peak = np.maximum.accumulate(equity)
    max_drawdown_money = float((peak - equity).max())
    max_drawdown_pct = float(((peak - equity) / peak).max())

    day_start, daily_r = _daily_r(trades, bars)
    return Metrics(
        trades=n,
        net_r=float(net),
        profit_factor=profit_factor(r),
        win_rate=float((r > 0).mean()) if n else np.nan,
        expectancy_r=float(r.mean()) if n else np.nan,
        top5_share=float(top5),
        max_drawdown_r=max_drawdown_r,
        day_start=day_start,
        daily_r=daily_r,
        equity=equity,
        max_drawdown_money=max_drawdown_money,
        max_drawdown_pct=max_drawdown_pct,
    )


def profit_factor(r: np.ndarray) -> float:
    """Gross winning R over gross losing R: inf with winners and no losers, NaN with neither."""
    r = np.asarray(r, dtype=np.float64)
    wins, losses = r[r > 0].sum(), -r[r < 0].sum()
    if losses == 0:
        return np.inf if wins > 0 else np.nan
    return float(wins / losses)


def _daily_r(trades: Trades, bars: Bars) -> tuple[np.ndarray, np.ndarray]:
    """One row per server trading day of the bars (any day with a bar), including zero days.

    A trade's R counts on the server day of its exit bar.
    """
    bar_days = (bars.time + server_offset(bars.time)) // DAY
    days, first = np.unique(bar_days, return_index=True)
    day_start = days * DAY - server_offset(bars.time[first])
    daily = np.zeros(len(days))
    if len(trades):
        exit_days = (trades["exit_time"] + server_offset(trades["exit_time"])) // DAY
        np.add.at(daily, np.searchsorted(days, exit_days), trades["r"])
    return day_start.astype(np.int64), daily
