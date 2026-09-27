"""Multi-timeframe inputs: what a higher timeframe knew at each lower-timeframe bar close.

A Strategy on H1 may be filtered by, say, "H4 close above its EMA50". The value it may use at an H1
close is the one from the last H4 bar that had already closed then, never the H4 bar still forming.
Everything that aligns timeframes goes through ``align_closed`` so that rule holds in one place.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def align_closed(lower_times: pd.Series, lower_tf_s: int, higher_times: pd.Series, higher_tf_s: int, values: pd.Series, fill=np.nan) -> pd.Series:
    """For each lower bar (by open time), the ``values`` of the last higher bar whose close is at or
    before the lower bar's close. Rows before the first higher close get ``fill``."""
    lower_close = lower_times.to_numpy(dtype=np.int64) + int(lower_tf_s)
    higher_close = higher_times.to_numpy(dtype=np.int64) + int(higher_tf_s)
    src = pd.DataFrame({"t": higher_close, "v": np.asarray(values)}).sort_values("t", kind="stable")
    dst = pd.DataFrame({"t": lower_close, "i": np.arange(len(lower_close))}).sort_values("t", kind="stable")
    merged = pd.merge_asof(dst, src, on="t", direction="backward").sort_values("i")
    out = merged["v"].to_numpy()
    if fill is not np.nan:
        out = pd.Series(out).fillna(fill).to_numpy()
    return pd.Series(out, index=lower_times.index)
