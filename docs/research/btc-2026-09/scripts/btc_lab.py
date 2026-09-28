"""BTCUSD research harness (read-only, offline). Runs signal frames through FXCommand's own PaperTrader
(next-bar-open fills, SL first when a bar touches both stops, gap fills).

Pricing (spec-btc-strategies D5, D6):
- spread per bar = max(today's $40 scaled by close / today's price, the bar's recorded spread), times 2
  (Evidence pricing); slippage $5 at today's price, scaled the same way;
- swap = the terminal's figures (long -3500.13, short -2333.42 points per lot per night), scaled by
  entry / today's price, charged for EVERY server midnight held (7 nights a week, no triple day).

Research window: signals from 2018-01-01; bars from the holdout (research.holdout_from, 2025-09-01)
onwards are never read. Periods: 2018-01..2020-06, 2020-07..2022-12, 2023-01..2025-08.
"""
import json
import pickle
import sys
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, r"D:\fxcommand\backend")
from fxcommand.learning.objective import deflation, r_stats  # noqa: E402
from fxcommand.learning.paper import ExitRules, PaperTrader  # noqa: E402
from fxcommand.learning.research import holdout_from  # noqa: E402
from fxcommand.strategies import indicators as ind  # noqa: E402

DAY = 86400
H = pickle.load(open(r"D:\fxcommand\backend\data\research\btc.pkl", "rb"))
SPEC = H["spec"]
PNOW = float(SPEC["price_now"])
SPREAD_NOW = 40.0
SLIP_NOW = 5.0
MULT = 2.0
POINT = float(SPEC["point"])
SWAP_NIGHT = {"long": SPEC["swap_long"] * POINT, "short": SPEC["swap_short"] * POINT}  # $ per 1.0 lot at PNOW
SEAL = holdout_from(int(SPEC["server_now"]), "H4")
START = int(pd.Timestamp("2018-01-01").timestamp())
CUTS = (int(pd.Timestamp("2020-07-01").timestamp()), int(pd.Timestamp("2023-01-01").timestamp()))
WARM = 400  # bars before START kept for indicator warm-up
BAR_S = {"M15": 900, "H1": 3600, "H4": 14400, "D1": DAY}
LEDGER: list[dict] = []


class BarCost:
    def __init__(self, spread: float, slip: float, swap_on: bool = True):
        self.spread, self.slip, self.swap_on = spread, slip, swap_on

    def spread_at(self, price, recorded=0.0):
        return self.spread

    def slippage_at(self, price):
        return self.slip

    def swap_for(self, side, entry_price, open_ts, close_ts):
        if not self.swap_on:
            return 0.0
        nights = close_ts // DAY - open_ts // DAY  # every server midnight in (open, close]
        return SWAP_NIGHT[side] * entry_price / PNOW * nights


def bars(tf: str) -> pd.DataFrame:
    df = H["data"][tf]
    df = df[df.time < SEAL].reset_index(drop=True)
    first = int(np.searchsorted(df.time.to_numpy(), START))
    return df.iloc[max(0, first - WARM):].reset_index(drop=True)


def frame(df, long, short, exit_long=None, exit_short=None, sl_atr=2.0, tp_atr=0.0, atr_period=14, sl=None, tp=None):
    a = ind.atr(df, atr_period)
    F = pd.Series(False, index=df.index)
    f = pd.DataFrame({
        "long": long.fillna(False).astype(bool), "short": short.fillna(False).astype(bool),
        "exit_long": (exit_long if exit_long is not None else F).fillna(False).astype(bool),
        "exit_short": (exit_short if exit_short is not None else F).fillna(False).astype(bool),
        "sl_dist": sl if sl is not None else a * sl_atr, "tp_dist": tp if tp is not None else a * tp_atr, "info_atr": a,
    })
    early = df.time < START
    f.loc[early, ["long", "short"]] = False
    return f


def run(df, fr, rules=None, mult=MULT, swap_on=True):
    t = df.time.to_numpy(np.int64)
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    bar_spread = df.spread.to_numpy(float) * POINT
    atr = fr["info_atr"].to_numpy(float)
    cols = {k: fr[k].to_numpy() for k in ("long", "short", "exit_long", "exit_short", "sl_dist", "tp_dist")}
    tr = PaperTrader(BarCost(0, 0), rules or ExitRules(reverse_on_opposite=False))
    out = []
    for i in range(len(df)):
        j = i - 1 if i else 0
        k = c[j] / PNOW
        tr.costs = BarCost(max(SPREAD_NOW * k, bar_spread[j]) * mult, SLIP_NOW * k, swap_on)
        _, closed = tr.on_bar(int(t[i]), o[i], h[i], l[i], c[i], atr[i] if atr[i] == atr[i] else 0.0, {kk: v[i] for kk, v in cols.items()})
        out.extend(closed)
    out.extend(tr.finish(int(t[-1]), float(c[-1])))
    return [x for x in out if x.signal_time >= START]


def stats(trades):
    rs = [x.r for x in trades]
    s = r_stats(rs)
    per = []
    for a, b in ((START, CUTS[0]), CUTS, (CUTS[1], SEAL)):
        per.append(r_stats([x.r for x in trades if a <= x.signal_time < b]))
    nights = np.mean([x.close_time // DAY - x.open_time // DAY for x in trades]) if trades else 0
    return s, per, nights


def score(name, family, df, fr, tf, rules=None, note=""):
    tr = run(df, fr, rules)
    s, per, nights = stats(tr)
    yrs = (SEAL - START) / 3.156e7
    row = dict(id=len(LEDGER) + 1, family=family, name=name, tf=tf, n=s.n, per_yr=round(s.n / yrs, 1), R=round(s.mean, 3), sqn=round(s.sqn, 2),
               win=round(s.win_rate, 2), dd=round(s.max_dd, 1), nights=round(float(nights), 2),
               p1=round(per[0].mean, 3), p2=round(per[1].mean, 3), p3=round(per[2].mean, 3),
               n1=per[0].n, n2=per[1].n, n3=per[2].n, long_share=round(np.mean([x.side == "long" for x in tr]), 2) if tr else None, note=note)
    LEDGER.append(row)
    print(json.dumps(row), flush=True)
    return row


def passes(row, trials):
    """The finalist bar before neighbours: >= 30 trades, positive in every period, SQN above deflation(trials)."""
    return row["n"] >= 30 and min(row["p1"], row["p2"], row["p3"]) > 0 and row["sqn"] > deflation(trials)


def save(path):
    json.dump(LEDGER, open(path, "w"), indent=1)


def hour(df):
    return pd.to_datetime(df.time, unit="s").dt.hour


def weekday(df):
    return pd.to_datetime(df.time, unit="s").dt.weekday
