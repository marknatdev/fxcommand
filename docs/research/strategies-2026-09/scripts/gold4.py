import sys; sys.path.insert(0, r"D:\fxcommand\backend\data\research")
from prop import *
from gold2 import ema_x, turtle, pdh
def line(df, fr):
    tr = run_prop(df, fr); rs = swap_prop(tr); P = periods(tr, rs); s = r_stats(rs)
    return s, P
rows = []
for tf in ("H4", "D1"):
    df = bars("GOLD", tf).reset_index(drop=True)
    for f in (5, 8, 10, 13, 15, 20):
        for s_ in (30, 40, 50, 60, 80, 100, 150, 200):
            if s_ <= 2 * f: continue
            for both in (False, True):
                for sl in (2.0, 3.0, 4.0):
                    s, P = line(df, ema_x(df, f, s_, both, sl)); rows.append(dict(tf=tf, fam="ema", a=f, b=s_, both=both, sl=sl, n=s.n, R=s.mean, p1=P[0].mean, p2=P[1].mean, p3=P[2].mean))
    for (ni, no) in ((20, 10), (40, 20), (55, 20), (100, 50)):
        for both in (False, True):
            for sl in (2.0, 3.0):
                s, P = line(df, turtle(df, ni, no, both, sl)); rows.append(dict(tf=tf, fam="turtle", a=ni, b=no, both=both, sl=sl, n=s.n, R=s.mean, p1=P[0].mean, p2=P[1].mean, p3=P[2].mean))
    print(tf, "done", flush=True)
R = pd.DataFrame(rows); R.to_csv(r"D:\fxcommand\backend\data\research\gold_prop.csv", index=False)
R["all3"] = (R.p1 > 0) & (R.p2 > 0) & (R.p3 > 0)
print(R.groupby(["tf", "fam", "both"]).agg(sets=("R", "size"), medR=("R", "median"), pos=("R", lambda x: (x > 0).mean()), all3=("all3", "mean"), med_p1=("p1", "median"), med_p2=("p2", "median"), med_p3=("p3", "median"), trades=("n", "median")).round(3).to_string())
