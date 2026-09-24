"""The Strategy interface: closed bars + params in, Signal out. Pure — no Broker, no money.

Each Strategy is written once, vectorised (ADR 0005): ``signals(bars, params)``
returns one row per bar with the columns in ``SIGNAL_COLUMNS``. Live trading reads
the last row (``run``); Backtests and Shadow Trades read every row. Exit columns
are separate for longs and shorts because whether a bar is an exit depends on the
position held, which the vectorised form cannot know.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Literal

import numpy as np
import pandas as pd

Action = Literal["long", "short", "exit", "none"]
PositionSide = Literal["long", "short"] | None

SIGNAL_COLUMNS = ("long", "short", "exit_long", "exit_short", "sl_dist", "tp_dist")

# Extra closed bars fed to live evaluation beyond a Strategy's lookback, so recursive
# indicators (EMA, Wilder) have converged and the last row matches a long Backtest series.
WARMUP_BARS = 300


@dataclass(frozen=True)
class Signal:
    action: Action
    reason: str
    sl_dist: float = 0.0  # price distance from entry to stop-loss (always > 0 for entries)
    tp_dist: float = 0.0  # price distance to take-profit (0 = none)
    bar_time: int = 0
    indicators: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def none(reason: str, bar_time: int = 0, **indicators: float) -> Signal:
    return Signal("none", reason, bar_time=bar_time, indicators=indicators)


@dataclass(frozen=True)
class Param:
    name: str
    label: str
    type: Literal["int", "float", "bool"]
    default: Any
    min: float | None = None
    max: float | None = None
    step: float | None = None
    help: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# explain(row, params, action) -> human-readable reason for the last bar
Explain = Callable[[pd.Series, dict, str], str]


@dataclass(frozen=True)
class Strategy:
    key: str
    title: str
    description: str
    params: tuple[Param, ...]
    lookback: Callable[[dict], int]  # closed bars needed for a valid evaluation
    compute: Callable[[pd.DataFrame, dict], pd.DataFrame]  # vectorised signals (SIGNAL_COLUMNS + info_* columns)
    explain: Explain

    def defaults(self) -> dict:
        return {p.name: p.default for p in self.params}

    def resolve(self, params: dict | None) -> dict:
        """Merge user params over defaults, coerce types, clamp to bounds. Unknown keys are dropped."""
        out = self.defaults()
        for p in self.params:
            if params is None or p.name not in params or params[p.name] is None:
                continue
            v = params[p.name]
            if p.type == "bool":
                v = bool(v)
            else:
                v = int(round(float(v))) if p.type == "int" else float(v)
                if p.min is not None:
                    v = max(v, type(v)(p.min))
                if p.max is not None:
                    v = min(v, type(v)(p.max))
            out[p.name] = v
        return out

    def signals(self, bars: pd.DataFrame, params: dict | None) -> pd.DataFrame:
        """One row per bar: entries, position-dependent exits and SL/TP distances. Rows before the
        lookback carry no signal."""
        p = self.resolve(params)
        bars = bars.reset_index(drop=True)
        frame = self.compute(bars, p)
        need = self.lookback(p)
        if len(frame) and need > 0:
            warm = np.arange(len(frame)) < need - 1
            for c in ("long", "short", "exit_long", "exit_short"):
                frame.loc[warm, c] = False
        return frame

    def run(self, bars: pd.DataFrame, params: dict | None, position: PositionSide = None) -> Signal:
        """Signal for the last closed bar, given the position currently held."""
        p = self.resolve(params)
        need = self.lookback(p)
        bars = bars.reset_index(drop=True)
        if len(bars) < need:
            return none(f"warming up: {len(bars)}/{need} bars", int(bars["time"].iloc[-1]) if len(bars) else 0)
        row = self.signals(bars, p).iloc[-1]
        return decide(row, p, position, int(bars["time"].iloc[-1]), self.explain)

    def describe(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "description": self.description,
            "params": [p.to_dict() for p in self.params],
        }


def decide(row: pd.Series, p: dict, position: PositionSide, bar_time: int, explain: Explain) -> Signal:
    info = {k[5:]: float(v) for k, v in row.items() if isinstance(k, str) and k.startswith("info_") and v == v}
    if position == "long" and bool(row["exit_long"]):
        return Signal("exit", explain(row, p, "exit_long"), bar_time=bar_time, indicators=info)
    if position == "short" and bool(row["exit_short"]):
        return Signal("exit", explain(row, p, "exit_short"), bar_time=bar_time, indicators=info)
    for side in ("long", "short"):
        if bool(row[side]):
            return Signal(
                side,  # type: ignore[arg-type]
                explain(row, p, side),
                sl_dist=float(row["sl_dist"]),
                tp_dist=float(row["tp_dist"]),
                bar_time=bar_time,
                indicators=info,
            )
    return Signal("none", explain(row, p, "none"), bar_time=bar_time, indicators=info)
