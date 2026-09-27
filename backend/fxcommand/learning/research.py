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


# ------------------------------------------------------------ research evaluation (the CLI)
MIN_RESEARCH_TRADES = 30
# Research is judged from 2010 (the spec's research periods 2010–17, 2018–21, 2022–26): older history
# (GOLD H4 reaches 2001) only warms indicators up, so a hypothesis cannot pass on years no period checks
RESEARCH_START = calendar.timegm((2010, 1, 1, 0, 0, 0))
RESEARCH_NEIGHBOURS = 4
NEIGHBOUR_SCALE = 0.12  # the Optimizer's own neighbourhood (optimizer._judge_robustness)


def _summary(trades, first: int, last: int) -> dict:
    from .evidence import periods

    trades = [t for t in trades if t.signal_time >= first]  # the bars before ``first`` are warm-up only
    rs = np.array([t.r for t in trades], dtype=float)
    close = np.array([t.close_time for t in trades], dtype=np.int64)
    st = r_stats(rs)
    years = max((last - first) / (365.25 * 86400), 1e-9)
    return {
        "trades": st.n, "mean_r": round(st.mean, 4), "sqn": round(st.sqn, 3), "win_rate": round(st.win_rate, 4),
        "total_r": round(st.total, 3), "max_dd_r": round(st.max_dd, 3), "trades_per_year": round(st.n / years, 1),
        "periods": periods(close, rs, first, last),
    }


def evaluate_hypothesis(
    bars: pd.DataFrame,
    candidate: Candidate,
    champion: Candidate | None,
    costs: CostModel,
    rules: ExitRules,
    gate: EntryGate,
    weekend: WeekendClose | None,
    trials: int,
    seed: int = 0,
) -> dict:
    """The Strategy Review's bar for a finalist (spec: Claude Strategy Review, step 5), on research bars:

    - enough trades to judge;
    - a positive mean R in **every** research period, and above the Champion's in each;
    - an SQN above max(Champion's, 0) plus the selection penalty for ``trials`` ledger trials;
    - every neighbouring parameter set (the Optimizer's neighbourhood) still positive.

    Only trades signalled from ``RESEARCH_START`` (2010) count; earlier bars warm indicators up.
    Win rate is reported, never optimised (D48)."""
    from .objective import deflation
    from .candidate import perturb

    first, last = max(int(bars["time"].iloc[0]), RESEARCH_START), int(bars["time"].iloc[-1])
    run = lambda c: _summary(backtest(bars, c, costs, rules, gate, weekend=weekend), first, last)  # noqa: E731
    cand = run(candidate)
    champ = run(champion) if champion is not None else None
    rng = np.random.default_rng(seed)
    neigh = []
    if cand["trades"]:
        for _ in range(RESEARCH_NEIGHBOURS):
            n = perturb(candidate, rng, NEIGHBOUR_SCALE)
            neigh.append({"params": n.param_dict, "mean_r": run(n)["mean_r"]})
    penalty = deflation(max(trials, 1))
    need_sqn = max(champ["sqn"] if champ else 0.0, 0.0) + penalty
    champ_periods = {p["label"]: p for p in champ["periods"]} if champ else {}
    weak = [
        p["label"] for p in cand["periods"]
        if p["n"] and (p["mean"] <= 0 or (p["label"] in champ_periods and p["mean"] <= champ_periods[p["label"]]["mean"]))
    ]
    empty = [p["label"] for p in cand["periods"] if not p["n"]]
    checks = [
        {"name": "trades", "ok": cand["trades"] >= MIN_RESEARCH_TRADES, "detail": f"{cand['trades']} trades (needs {MIN_RESEARCH_TRADES})"},
        {"name": "every_period", "ok": not weak and not empty,
         "detail": "positive and above the Champion in every period" if not weak and not empty
         else f"fails in {', '.join(weak + [f'{e} (no trades)' for e in empty])}"},
        {"name": "selection_penalty", "ok": cand["sqn"] > need_sqn,
         "detail": f"SQN {cand['sqn']:.2f} vs max(Champion {champ['sqn'] if champ else 0:.2f}, 0) + penalty {penalty:.2f} for {trials} ledger trials"},
        {"name": "neighbours", "ok": bool(neigh) and all(n["mean_r"] > 0 for n in neigh),
         "detail": "neighbouring parameters " + ", ".join(f"{n['mean_r']:+.3f}R" for n in neigh) if neigh else "no trades"},
    ]
    return {
        "candidate": cand, "champion": champ, "neighbours": neigh, "checks": checks,
        "finalist": all(c["ok"] for c in checks), "trials": trials, "penalty": round(penalty, 3),
        "bars": int(len(bars)), "first_ts": first, "last_ts": last,
    }
