import sys, warnings; warnings.filterwarnings("ignore"); sys.path.insert(0, r"D:\fxcommand\backend\data\research")
from gold2 import *
for tf in ("H4", "D1"):
    df = bars("GOLD", tf).reset_index(drop=True); cells = []
    for f in (5, 8, 10, 13, 15, 20):
        for s in (30, 40, 50, 60, 80, 100):
            if s <= 2 * f: continue
            for sl in (2.0, 3.0, 4.0):
                _, a, b = ev(df, ema_x(df, f, s, True, sl)); cells.append((f, s, sl, a, b))
    A = np.array([(a, b) for *_, a, b in cells])
    print(f"GOLD {tf} EMA L+S grid ({len(cells)} sets, swap+spread): both halves>0 {np.mean((A[:,0]>0)&(A[:,1]>0)):.0%}; median 2010-17 {np.median(A[:,0]):+.3f}R, 2018-26 {np.median(A[:,1]):+.3f}R")
    best = sorted(cells, key=lambda c: min(c[3], c[4]), reverse=True)[:5]
    for c in best: print("   ", c[:3], f"{c[3]:+.3f} / {c[4]:+.3f}")
