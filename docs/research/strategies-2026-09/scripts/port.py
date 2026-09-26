import sys; sys.path.insert(0, r"D:\fxcommand\backend\data\research")
from lab import *
from robust import with_swap
from study import ema_trend, turtle
U = ["EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD","USDCHF","NZDUSD","EURJPY","GBPJPY","EURGBP","GOLD","SILVER","OILCash","US500Cash","US100Cash","GER40Cash","JP225Cash"]
for tf in ("H4", "D1"):
    for name, fn in (("ema50/200", ema_trend), ("turtle55/20", turtle)):
        allr, per = [], []
        for sym in U:
            df = bars(sym, tf)
            if len(df) < 600: continue
            tr = run_frame(df, fn(df), costs_for(sym, df, 2)); rs = with_swap(tr, sym)
            allr += list(zip([t.open_time for t in tr], rs)); per.append((sym, round(np.mean(rs), 3) if rs else None))
        allr.sort(); rs = [r for _, r in allr]; s = r_stats(rs); mid = len(rs)//2
        yrs = 16
        print(f"{tf} {name:11s} portfolio n={s.n} ({s.n/yrs:.0f}/yr) meanR={s.mean:+.3f} halves={np.mean(rs[:mid]):+.3f}/{np.mean(rs[mid:]):+.3f} maxDD={s.max_dd:.0f}R  positive syms={sum(1 for _,m in per if m and m>0)}/{len(per)}", flush=True)
