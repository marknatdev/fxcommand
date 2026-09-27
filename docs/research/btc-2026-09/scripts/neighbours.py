"""Round 1 follow-up: is Turtle 100/50 long H4 a plateau? Its neighbourhood (entry, exit, stop each
±12% and ±25%), and cost sensitivity. Neighbours and diagnostics judge the one candidate; they are
not new hypotheses (the ledger count stays 24)."""
import itertools

from btc_lab import *  # noqa: F403
from families import turtle, h4  # noqa: F401  (re-runs round 1 and rewrites its ledger)

base = LEDGER[:]
LEDGER.clear()
rows = []
for ni, no, sl in itertools.product((75, 88, 100, 112, 125), (38, 44, 50, 56, 63), (1.5, 1.75, 2.0, 2.25, 2.5)):
    tr = run(h4, turtle(h4, ni, no, "L", sl))
    s, per, nights = stats(tr)
    rows.append(dict(ni=ni, no=no, sl=sl, n=s.n, R=round(s.mean, 3), sqn=round(s.sqn, 2), p1=round(per[0].mean, 3), p2=round(per[1].mean, 3), p3=round(per[2].mean, 3)))
nb = pd.DataFrame(rows)
nb.to_csv(r"D:\fxcommand\docs\research\btc-2026-09\results\turtle_neighbours.csv", index=False)
near = nb[nb.ni.isin((88, 100, 112)) & nb.no.isin((44, 50, 56)) & nb.sl.isin((1.75, 2.0, 2.25))]
print("±12% neighbourhood:", len(near), "sets; positive overall:", int((near.R > 0).sum()), "; positive in every period:", int((near[["p1", "p2", "p3"]].min(axis=1) > 0).sum()))
print("±25% grid:", len(nb), "sets; positive overall:", int((nb.R > 0).sum()), "; positive in every period:", int((nb[["p1", "p2", "p3"]].min(axis=1) > 0).sum()))
print("worst ±12% neighbour:\n", near.sort_values("R").head(3).to_string(index=False))
print("p1 by entry length (median over exits, stops):\n", nb.groupby("ni")[["R", "p1", "p2", "p3"]].median().to_string())

fr = turtle(h4, 100, 50, "L")
for label, kw in (("1x spread", dict(mult=1.0)), ("2x spread, no swap", dict(swap_on=False)), ("4x spread", dict(mult=4.0)), ("zero cost", dict(mult=0.0, swap_on=False))):
    s, per, nights = stats(run(h4, fr, **kw))
    print(f"{label:>20}: n={s.n} R={s.mean:.3f} p=({per[0].mean:.3f}, {per[1].mean:.3f}, {per[2].mean:.3f})")
tr = run(h4, fr)
rs = np.array([x.r for x in tr])
print("share of total R from the best 5 trades:", round(np.sort(rs)[-5:].sum() / rs.sum(), 2), " median R:", round(float(np.median(rs)), 3))
yr = pd.Series(rs, index=pd.to_datetime([x.signal_time for x in tr], unit="s").year).groupby(level=0).agg(["count", "mean", "sum"]).round(2)
print(yr.to_string())
