"""Costs proportional to price: today's GOLD spread/slippage/swap scaled by close/today's price."""
import sys, warnings; warnings.filterwarnings("ignore"); sys.path.insert(0, r"D:\fxcommand\backend\data\research")
from lab import *
from robust import swap_price_per_night
PNOW = 4264.0

def run_prop(df, fr, spread=0.57, slip=0.05, rules=None):
    t = df["time"].to_numpy(np.int64); o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    atr = fr["info_atr"].to_numpy(float)
    cols = {k: fr[k].to_numpy() for k in ("long", "short", "exit_long", "exit_short", "sl_dist", "tp_dist")}
    tr = PaperTrader(Costs(spread, slip), rules or ExitRules()); out = []
    for i in range(len(df)):
        k = c[i - 1] / PNOW if i else c[0] / PNOW
        tr.costs = Costs(spread * k, slip * k)
        _, closed = tr.on_bar(int(t[i]), o[i], h[i], l[i], c[i], atr[i] if atr[i] == atr[i] else 0.0, {k2: v[i] for k2, v in cols.items()})
        out.extend(closed)
    out.extend(tr.finish(int(t[-1]), float(c[-1])))
    return out

def swap_prop(trades, sym="GOLD"):
    out = []
    for x in trades:
        if x.r == 0: out.append(0.0); continue
        risk = abs(x.exit - x.entry) / abs(x.r)
        nights = max(0, x.close_time // 86400 - x.open_time // 86400)
        out.append(x.r + swap_price_per_night(sym, x.side) * (x.entry / PNOW) * nights / risk)
    return out

def periods(trades, rs, cuts=("2018-01-01", "2022-01-01")):
    c1, c2 = (int(pd.Timestamp(c).timestamp()) for c in cuts)
    g = [[r for x, r in zip(trades, rs) if x.open_time < c1], [r for x, r in zip(trades, rs) if c1 <= x.open_time < c2], [r for x, r in zip(trades, rs) if x.open_time >= c2]]
    return [r_stats(v) for v in g]
