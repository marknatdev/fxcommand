"""Offline research harness: run signal frames through FXCommand's own PaperTrader (same fill/stop rules as Shadow Trades)."""
import pickle, math, sys
import numpy as np, pandas as pd
sys.path.insert(0, r"D:\fxcommand\backend")
from fxcommand.learning.paper import PaperTrader, Costs, ExitRules
from fxcommand.learning.objective import r_stats
from fxcommand.strategies import indicators as ind
from fxcommand.strategies import get_strategy

S = r"D:\fxcommand\backend\data\research"
H = pickle.load(open(S + r"\history.pkl", "rb"))
DATA, SPECS = H["data"], H["specs"]

def bars(sym, tf):
    df = DATA[(sym, tf)].copy()
    if not len(df): return pd.DataFrame(columns=["time","open","high","low","close","tick_volume","spread"])
    if len(df): df = df[df.time >= 1262304000]  # 2010+ only (older series are not XM-quoted)
    return df[["time","open","high","low","close","tick_volume","spread"]].reset_index(drop=True)

def costs_for(sym, df=None, mult=1.0):
    sp = SPECS[sym]
    pts = sp["spread_pts"]
    if (not pts) and df is not None and len(df):
        pts = float(np.median(df["spread"].tail(2000)))
    return Costs(spread=pts * sp["point"] * mult, slippage=sp["point"])

def run_frame(df, frame, costs, rules=None, allow=None):
    t = df["time"].to_numpy(np.int64); o,h,l,c = (df[k].to_numpy(float) for k in ("open","high","low","close"))
    atr = frame["info_atr"].to_numpy(float)
    cols = {k: frame[k].to_numpy() for k in ("long","short","exit_long","exit_short","sl_dist","tp_dist")}
    tr = PaperTrader(costs, rules or ExitRules(), allow); out = []
    for i in range(len(df)):
        sig = {k: v[i] for k, v in cols.items()}
        _, closed = tr.on_bar(int(t[i]), o[i], h[i], l[i], c[i], atr[i] if atr[i] == atr[i] else 0.0, sig)
        out.extend(closed)
    out.extend(tr.finish(int(t[-1]), float(c[-1])))
    return out

def frame(df, long, short, exit_long=None, exit_short=None, atr_period=14, sl_atr=2.0, tp_atr=0.0):
    a = ind.atr(df, atr_period); F = pd.Series(False, index=df.index)
    f = pd.DataFrame({"long": long.fillna(False).astype(bool), "short": short.fillna(False).astype(bool),
        "exit_long": (exit_long if exit_long is not None else F).fillna(False).astype(bool),
        "exit_short": (exit_short if exit_short is not None else F).fillna(False).astype(bool),
        "sl_dist": a * sl_atr, "tp_dist": a * tp_atr, "info_atr": a})
    f.loc[:250, ["long","short"]] = False
    return f

def summary(trades, df):
    s = r_stats(t.r for t in trades)
    mid = int(df["time"].iloc[len(df)//2])
    a = r_stats(t.r for t in trades if t.open_time < mid); b = r_stats(t.r for t in trades if t.open_time >= mid)
    yrs = (df["time"].iloc[-1] - df["time"].iloc[0]) / 3.15e7
    return dict(n=s.n, per_yr=round(s.n / max(yrs, 1e-9), 1), meanR=round(s.mean, 3), sqn=round(s.sqn, 2), win=round(s.win_rate, 2),
                dd=round(s.max_dd, 1), h1R=round(a.mean, 3), h2R=round(b.mean, 3))
