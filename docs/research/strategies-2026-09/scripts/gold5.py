import sys; sys.path.insert(0, r"D:\fxcommand\backend\data\research")
from prop import *
def session_long(df, bar_min, entry_hm, exit_hm, sl, atr_n):
    t = pd.to_datetime(df.time, unit="s"); m = t.dt.hour * 60 + t.dt.minute; wd = t.dt.weekday
    nxt = m.shift(-1); same_day = t.dt.normalize().shift(-1) == t.dt.normalize()
    long = (nxt == entry_hm) & (wd.shift(-1) < 5)
    ex = (nxt == exit_hm)
    F = pd.Series(False, index=df.index)
    return frame(df, long, F, ex, None, atr_n, sl, 0)
print("H1 (2010-2026), proportional costs; periods 2010-17 | 2018-21 | 2022-26")
df = bars("GOLD", "H1").reset_index(drop=True)
for ent in (60, 120):
    for ex in (120, 180, 240, 300):
        if ex <= ent: continue
        for sl in (1.0, 2.0, 3.0):
            tr = run_prop(df, session_long(df, 60, ent, ex, sl, 24), spread=0.70 if ent == 60 else 0.57); rs = swap_prop(tr); P = periods(tr, rs)
            print(f"  in {ent//60:02d}:00 out {ex//60:02d}:00 sl{sl}: " + " | ".join(f"n={p.n} {p.mean:+.3f}" for p in P))
print("M15 (2022-07..2026), today's absolute costs")
df = bars("GOLD", "M15").reset_index(drop=True)
for ent, sp in ((60, 0.70), (75, 0.58), (90, 0.57)):
    for ex in (120, 180, 240):
        for sl in (1.0, 2.0, 3.0):
            fr = session_long(df, 15, ent, ex, sl, 96)
            tr = run_frame(df, fr, Costs(spread=sp, slippage=0.05)); s = r_stats(x.r for x in tr)
            mid = int(df.time.iloc[len(df)//2]); a = r_stats(x.r for x in tr if x.open_time < mid); b = r_stats(x.r for x in tr if x.open_time >= mid)
            print(f"  in {ent//60:02d}:{ent%60:02d} out {ex//60:02d}:00 sl{sl}: n={s.n} R={s.mean:+.3f} sqn={s.sqn:+.2f} halves {a.mean:+.3f}/{b.mean:+.3f}")
