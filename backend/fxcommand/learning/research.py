"""Claude Strategy Review support (spec D44, D47): the sealed holdout, the trial ledger's identity of
a hypothesis, and the one-time holdout evaluation. Pure.

The sealed holdout is the most recent 12 months of an Arena (3 months for M1/M5, whose history is
short), from the start of a calendar month in server time, so it rolls forward monthly. Research
never sees it: the snapshot ends before it, and a trial whose data reaches it is refused. A finalist
is scored on it once; the use is recorded in the ledger and a failure is final.
"""

from __future__ import annotations

import calendar
import time

import numpy as np
import pandas as pd

from ..risk.window import WeekendClose
from .candidate import Candidate
from .costs import CostModel
from .evidence import settings_hash
from .objective import r_stats
from .paper import EntryGate, ExitRules, backtest

HOLDOUT_MONTHS = 12
SCALP_HOLDOUT_MONTHS = 3
SCALP_TIMEFRAMES = ("M1", "M5")
HOLDOUT_MIN_TRADES = 5  # GOLD Trend takes about 9 trades a year: a higher bar would fail every H4 finalist
HOLDOUT_RULE = (
    f"at least {HOLDOUT_MIN_TRADES} trades opened in the holdout, a positive mean R after costs and swap "
    "(2× the typical spread), and a mean R at least the Champion's on the same bars"
)


def holdout_from(now: int, timeframe: str) -> int:
    """Server time at which an Arena's sealed holdout starts: the first day of the month
    ``HOLDOUT_MONTHS`` (M1/M5: ``SCALP_HOLDOUT_MONTHS``) before the current month."""
    months = SCALP_HOLDOUT_MONTHS if str(timeframe) in SCALP_TIMEFRAMES else HOLDOUT_MONTHS
    t = time.gmtime(now)
    idx = t.tm_year * 12 + (t.tm_mon - 1) - months
    return calendar.timegm((idx // 12, idx % 12 + 1, 1, 0, 0, 0))


def research_bars(bars: pd.DataFrame, cutoff: int, bar_seconds: int) -> pd.DataFrame:
    """Only bars that closed before the holdout starts."""
    if len(bars) == 0:
        return bars
    return bars[bars["time"].to_numpy(dtype=np.int64) + int(bar_seconds) <= cutoff].reset_index(drop=True)


def hypothesis_hash(strategy: str | None, params: dict | None, hypothesis: str) -> str:
    """The ledger's identity of what was tried: the Candidate key when it is a parameter set of an
    existing Strategy, otherwise a hash of the hypothesis text (new code)."""
    if strategy:
        return Candidate.of(strategy, params).key
    return settings_hash({"hypothesis": hypothesis.strip().lower()})


def _stats(trades, cutoff: int) -> dict:
    rs = [t.r for t in trades if t.signal_time >= cutoff]
    st = r_stats(rs)
    return {"trades": st.n, "mean_r": round(st.mean, 4), "sqn": round(st.sqn, 3), "win_rate": round(st.win_rate, 4),
            "total_r": round(st.total, 3), "max_dd_r": round(st.max_dd, 3)}


def holdout_job(
    bars: pd.DataFrame,
    candidate: Candidate,
    champion: Candidate | None,
    costs: CostModel,
    rules: ExitRules,
    gate: EntryGate,
    weekend: WeekendClose | None,
    cutoff: int,
) -> dict:
    """Backtest on the warm-up plus the holdout; only trades whose Signal came after ``cutoff`` count.
    Picklable (learning process)."""
    cand = _stats(backtest(bars, candidate, costs, rules, gate, weekend=weekend), cutoff)
    champ = _stats(backtest(bars, champion, costs, rules, gate, weekend=weekend), cutoff) if champion is not None else None
    passed = cand["trades"] >= HOLDOUT_MIN_TRADES and cand["mean_r"] > 0 and (champ is None or cand["mean_r"] >= champ["mean_r"])
    return {"candidate": cand, "champion": champ, "passed": bool(passed), "rule": HOLDOUT_RULE, "bars": int(len(bars))}
