"""The signal frame every Strategy returns: one row per bar, SIGNAL_COLUMNS plus info_* columns."""

from __future__ import annotations

import pandas as pd

from . import indicators as ind


def signal_frame(bars: pd.DataFrame, p: dict, long, short, exit_long=None, exit_short=None, **info) -> pd.DataFrame:
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
