"""The GOLD pair (spec-better-strategies.md): Trend Breakout on GOLD H4 and GOLD Reopen Drift on H1.

Both passed the research bar on the operator's XM history: positive in every period after
price-scaled spread, slippage and swap, and on a sealed holdout year. See
docs/research/strategies-2026-09/. Trend Breakout also runs as BTC Trend on BTCUSD H4 (entry 100,
exit 50; spec-btc-strategies.md, docs/research/btc-2026-09/).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import indicators as ind
from .base import Param, Strategy
from .frame import signal_frame

DAY = 86400


def _fmt(v: float) -> str:
    return f"{v:.5g}"


# ------------------------------------------------------------------ Trend Breakout
def _trend_breakout(bars: pd.DataFrame, p: dict) -> pd.DataFrame:
    upper, lower = ind.donchian(bars, p["entry"])
    x_upper, x_lower = ind.donchian(bars, p["exit"])
    close = bars["close"]
    long, exit_long = close > upper, close < x_lower
    if p["allow_short"]:
        short, exit_short = close < lower, close > x_upper
    else:
        short = exit_short = None
    return signal_frame(bars, p, long, short, exit_long, exit_short, upper=upper, lower=lower, exit_lower=x_lower, exit_upper=x_upper)


def _trend_explain(row: pd.Series, p: dict, action: str) -> str:
    return {
        "long": f"close above the {p['entry']}-bar high {_fmt(row['info_upper'])}",
        "short": f"close below the {p['entry']}-bar low {_fmt(row['info_lower'])}",
        "exit_long": f"close below the {p['exit']}-bar low {_fmt(row['info_exit_lower'])}",
        "exit_short": f"close above the {p['exit']}-bar high {_fmt(row['info_exit_upper'])}",
    }.get(action, "no breakout")


TREND_BREAKOUT = Strategy(
    key="trend_breakout",
    title="Trend Breakout",
    description="Turtle-style breakout for trending markets (tested on GOLD H4 at 55/20 and BTCUSD H4 at 100/50). Goes long when the close breaks "
    "the highest high of the previous N bars and exits when it breaks the lowest low of a shorter channel. "
    "Long-only unless shorts are allowed.",
    params=(
        Param("entry", "Entry channel (bars)", "int", 55, 5, 400, 1, search_min=40, search_max=100),
        Param("exit", "Exit channel (bars)", "int", 20, 2, 400, 1, search_min=10, search_max=50),
        Param("atr_period", "ATR period", "int", 20, 2, 200, 1, search_min=20, search_max=20),
        Param("sl_atr", "Stop-loss (× ATR)", "float", 2.0, 0.2, 20, 0.1, search_min=2.0, search_max=3.0),
        Param("tp_atr", "Take-profit (× ATR, 0 = none)", "float", 0.0, 0, 50, 0.1, search_min=0.0, search_max=0.0),
        Param("allow_short", "Allow shorts", "bool", False, help="Off by default: shorts lost money in 2018–21 on GOLD"),
    ),
    lookback=lambda p: max(p["entry"], p["exit"], p["atr_period"]) + 2,
    compute=_trend_breakout,
    explain=_trend_explain,
    family="gold",
)


# ------------------------------------------------------------ GOLD Reopen Drift
def _bar_seconds(times: pd.Series) -> int:
    d = np.diff(times.to_numpy(dtype=np.int64))
    d = d[d > 0]
    return int(np.min(d)) if len(d) else 3600  # the smallest step is the timeframe; gaps are larger


def _weekday(day: np.ndarray) -> np.ndarray:
    """0 = Monday … 6 = Sunday, for epoch day numbers (1970-01-01 was a Thursday)."""
    return (day + 3) % 7


def next_entry_time(close_ts: np.ndarray, entry_hour: int) -> np.ndarray:
    """The first ``entry_hour``:00 at or after each bar close, on a Monday–Friday (server time)."""
    day, sec = close_ts // DAY, close_ts % DAY
    target = entry_hour * 3600
    day = np.where(sec <= target, day, day + 1)
    wd = _weekday(day)
    day = day + np.where(wd == 5, 2, np.where(wd == 6, 1, 0))
    return day * DAY + target


def _weekend_days_between(day_a: np.ndarray, day_b: np.ndarray) -> np.ndarray:
    """Saturdays and Sundays in [day_a, day_b)."""
    out = np.zeros(len(day_a), dtype=np.int64)
    span = int(np.max(day_b - day_a)) if len(day_a) else 0
    for k in range(max(span, 0)):
        d = day_a + k
        out += ((d < day_b) & (_weekday(d) >= 5)).astype(np.int64)
    return out


def _session_drift(bars: pd.DataFrame, p: dict) -> pd.DataFrame:
    times = bars["time"].astype(np.int64)
    tf = _bar_seconds(times)
    close_t = times.to_numpy() + tf
    entry_at = next_entry_time(close_t, p["entry_hour"])
    # how long before the entry time this bar closed, not counting the weekend closure
    lead = entry_at - close_t - DAY * _weekend_days_between(close_t // DAY, entry_at // DAY)
    fire = pd.Series((lead >= 0) & (lead < tf + p["max_gap_min"] * 60), index=bars.index)
    # only the first qualifying bar per entry time fires
    first = fire & (fire.groupby(entry_at).cumsum() == 1)
    minute = (close_t % DAY) // 60
    exit_long = pd.Series(minute >= p["exit_hour"] * 60, index=bars.index)
    return signal_frame(bars, p, first, None, exit_long, None, entry_at=pd.Series(entry_at.astype(float), index=bars.index))


def _drift_explain(row: pd.Series, p: dict, action: str) -> str:
    if action == "long":
        return f"buy the {p['entry_hour']:02d}:00 reopen (server time)"
    if action == "exit_long":
        return f"{p['exit_hour']:02d}:00 reached: close the reopen trade"
    return "outside the reopen"


SESSION_DRIFT = Strategy(
    key="session_drift",
    title="GOLD Reopen Drift",
    description="Buys GOLD at the daily reopen (01:00 server time) and sells a few hours later, never holding "
    "overnight. The order is sent at the first tick after the reopen and dropped if it cannot be filled "
    "within a few minutes, because the edge fades quickly.",
    params=(
        Param("entry_hour", "Entry hour (server time)", "int", 1, 0, 23, 1, search_min=1, search_max=1),
        Param("exit_hour", "Exit hour (server time)", "int", 4, 1, 23, 1, search_min=3, search_max=5),
        Param("max_gap_min", "Market-closed gap before the entry (minutes)", "int", 60, 0, 720, 1,
              search_min=60, search_max=60, help="The signal bar may close this long before the entry hour (the daily break)"),
        Param("atr_period", "ATR period", "int", 24, 2, 200, 1, search_min=24, search_max=24),
        Param("sl_atr", "Stop-loss (× ATR)", "float", 1.5, 0.2, 20, 0.1, search_min=1.0, search_max=2.0),
        Param("tp_atr", "Take-profit (× ATR, 0 = none)", "float", 0.0, 0, 50, 0.1, search_min=0.0, search_max=0.0),
    ),
    lookback=lambda p: 3 * p["atr_period"] + 2,  # enough for the Wilder ATR to settle
    compute=_session_drift,
    explain=_drift_explain,
    family="gold",
    fill_window_s=300,
)
