"""The v1 Strategy catalog: EMA Cross, Donchian Breakout, RSI Mean Reversion.

Each is vectorised (see ``base``): one column per decision, evaluated for every
bar; SL/TP distances are ATR multiples.
"""

from __future__ import annotations

import pandas as pd

from . import indicators as ind
from .base import Param, Strategy


def _frame(bars: pd.DataFrame, p: dict, long, short, exit_long=None, exit_short=None, **info) -> pd.DataFrame:
    a = ind.atr(bars, p["atr_period"])
    false = pd.Series(False, index=bars.index)
    f = pd.DataFrame(
        {
            "long": long.fillna(False).astype(bool) if long is not None else false,
            "short": short.fillna(False).astype(bool) if short is not None else false,
            "exit_long": exit_long.fillna(False).astype(bool) if exit_long is not None else false,
            "exit_short": exit_short.fillna(False).astype(bool) if exit_short is not None else false,
            "sl_dist": a * p["sl_atr"],
            "tp_dist": a * p["tp_atr"],
            "info_atr": a,
        },
        index=bars.index,
    )
    for k, v in info.items():
        f[f"info_{k}"] = v
    return f


def _fmt(v: float) -> str:
    return f"{v:.5g}"


# --------------------------------------------------------------------- EMA Cross
def _ema_cross(bars: pd.DataFrame, p: dict) -> pd.DataFrame:
    close = bars["close"]
    fast, slow = ind.ema(close, p["fast"]), ind.ema(close, p["slow"])
    up = (fast.shift(1) <= slow.shift(1)) & (fast > slow)
    down = (fast.shift(1) >= slow.shift(1)) & (fast < slow)
    info = {"ema_fast": fast, "ema_slow": slow}
    if p["trend_filter"]:
        trend = ind.ema(close, p["trend_ema"])
        info["ema_trend"] = trend
        f = _frame(bars, p, up & (close > trend), down & (close < trend), **info)
        f["cross_up"], f["cross_down"] = up, down
    else:
        f = _frame(bars, p, up, down, **info)
        f["cross_up"], f["cross_down"] = up, down
    return f


def _ema_explain(row: pd.Series, p: dict, action: str) -> str:
    if action == "long":
        return f"EMA{p['fast']} crossed above EMA{p['slow']}"
    if action == "short":
        return f"EMA{p['fast']} crossed below EMA{p['slow']}"
    if bool(row.get("cross_up")):
        return "bullish cross rejected by trend filter"
    if bool(row.get("cross_down")):
        return "bearish cross rejected by trend filter"
    return "no cross"


EMA_CROSS = Strategy(
    key="ema_cross",
    title="EMA Cross",
    description="Goes long when the fast EMA crosses above the slow EMA and short on the opposite cross. "
    "Optional trend filter only takes trades in the direction of a long-period EMA.",
    params=(
        Param("fast", "Fast EMA", "int", 9, 2, 200, 1),
        Param("slow", "Slow EMA", "int", 21, 3, 400, 1),
        Param("trend_filter", "Trend filter", "bool", False),
        Param("trend_ema", "Trend EMA", "int", 200, 20, 1000, 1),
        Param("atr_period", "ATR period", "int", 14, 2, 200, 1),
        Param("sl_atr", "Stop-loss (× ATR)", "float", 1.5, 0.2, 20, 0.1),
        Param("tp_atr", "Take-profit (× ATR, 0 = none)", "float", 3.0, 0, 50, 0.1),
    ),
    lookback=lambda p: max(p["slow"], p["trend_ema"] if p["trend_filter"] else 0, p["atr_period"]) + 3,
    compute=_ema_cross,
    explain=_ema_explain,
)


# ------------------------------------------------------------ Donchian Breakout
def _donchian(bars: pd.DataFrame, p: dict) -> pd.DataFrame:
    upper, lower = ind.donchian(bars, p["period"])
    close = bars["close"]
    if p["exit_period"] > 0:
        xu, xl = ind.donchian(bars, p["exit_period"])
        exit_long, exit_short = close < xl, close > xu
    else:
        exit_long = exit_short = None
    return _frame(bars, p, close > upper, close < lower, exit_long, exit_short, upper=upper, lower=lower)


def _donchian_explain(row: pd.Series, p: dict, action: str) -> str:
    return {
        "exit_long": f"close below {p['exit_period']}-bar low",
        "exit_short": f"close above {p['exit_period']}-bar high",
        "long": f"breakout above {p['period']}-bar high {_fmt(row['info_upper'])}",
        "short": f"breakdown below {p['period']}-bar low {_fmt(row['info_lower'])}",
    }.get(action, "inside channel")


DONCHIAN = Strategy(
    key="donchian_breakout",
    title="Donchian Breakout",
    description="Enters when the close breaks the highest high / lowest low of the previous N bars. "
    "Exits early when price breaks the shorter exit channel against the position.",
    params=(
        Param("period", "Entry channel (bars)", "int", 20, 5, 400, 1),
        Param("exit_period", "Exit channel (bars, 0 = off)", "int", 10, 0, 400, 1),
        Param("atr_period", "ATR period", "int", 14, 2, 200, 1),
        Param("sl_atr", "Stop-loss (× ATR)", "float", 2.0, 0.2, 20, 0.1),
        Param("tp_atr", "Take-profit (× ATR, 0 = none)", "float", 3.0, 0, 50, 0.1),
    ),
    lookback=lambda p: max(p["period"], p["exit_period"], p["atr_period"]) + 2,
    compute=_donchian,
    explain=_donchian_explain,
)


# --------------------------------------------------------- RSI Mean Reversion
def _rsi_reversion(bars: pd.DataFrame, p: dict) -> pd.DataFrame:
    r = ind.rsi(bars["close"], p["rsi_period"])
    a = ind.adx(bars, p["adx_period"])
    r0 = r.shift(1)
    up = (r0 < p["oversold"]) & (p["oversold"] <= r)
    down = (r0 > p["overbought"]) & (p["overbought"] >= r)
    ranging = a < p["adx_max"]
    if p["exit_at_mid"]:
        exit_long, exit_short = r >= 50, r <= 50
    else:
        exit_long = exit_short = None
    f = _frame(bars, p, up & ranging, down & ranging, exit_long, exit_short, rsi=r, adx=a)
    f["rsi_up"], f["rsi_down"] = up, down
    return f


def _rsi_explain(row: pd.Series, p: dict, action: str) -> str:
    if action in ("exit_long", "exit_short"):
        return "RSI back to 50"
    if action == "long":
        return f"RSI crossed up through {p['oversold']}"
    if action == "short":
        return f"RSI crossed down through {p['overbought']}"
    adx_now = float(row["info_adx"])
    if bool(row.get("rsi_up")):
        return f"oversold bounce ignored: ADX {adx_now:.1f} ≥ {p['adx_max']} (trending)"
    if bool(row.get("rsi_down")):
        return f"overbought fade ignored: ADX {adx_now:.1f} ≥ {p['adx_max']} (trending)"
    return "no RSI trigger"


RSI_REVERSION = Strategy(
    key="rsi_reversion",
    title="RSI Mean Reversion",
    description="Fades extremes in ranging markets: buys when RSI climbs back above oversold, sells when it "
    "drops back below overbought — only while ADX says the market is not trending.",
    params=(
        Param("rsi_period", "RSI period", "int", 14, 2, 100, 1),
        Param("oversold", "Oversold", "float", 30, 1, 49, 1),
        Param("overbought", "Overbought", "float", 70, 51, 99, 1),
        Param("adx_period", "ADX period", "int", 14, 2, 100, 1),
        Param("adx_max", "Max ADX (ranging)", "float", 25, 5, 100, 1),
        Param("exit_at_mid", "Exit when RSI returns to 50", "bool", True),
        Param("atr_period", "ATR period", "int", 14, 2, 200, 1),
        Param("sl_atr", "Stop-loss (× ATR)", "float", 1.5, 0.2, 20, 0.1),
        Param("tp_atr", "Take-profit (× ATR, 0 = none)", "float", 2.0, 0, 50, 0.1),
    ),
    lookback=lambda p: max(p["rsi_period"], p["adx_period"] * 3, p["atr_period"]) + 3,
    compute=_rsi_reversion,
    explain=_rsi_explain,
)


STRATEGIES: dict[str, Strategy] = {s.key: s for s in (EMA_CROSS, DONCHIAN, RSI_REVERSION)}


def get_strategy(key: str) -> Strategy:
    try:
        return STRATEGIES[key]
    except KeyError:
        raise KeyError(f"unknown strategy {key!r}; available: {', '.join(STRATEGIES)}") from None

