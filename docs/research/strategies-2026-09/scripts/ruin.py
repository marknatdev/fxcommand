import sys; sys.path.insert(0, r"D:\fxcommand\backend\data\research")
from lab import *
from robust import connors_p, with_swap
sym = "US500Cash"; df = bars(sym, "D1")
tr = run_frame(df, connors_p(10, 200, 5, True, 3.0)(df), costs_for(sym, df, 2))
R = np.array(with_swap(tr, sym)); sp = SPECS[sym]
stop_usd = 3 * float(ind.atr(df, 14).iloc[-1]) * sp["tick_value"] / sp["tick_size"] * sp["vmin"]
print(f"trades={len(R)} meanR={R.mean():+.3f} win={np.mean(R>0):.0%} worst={R.min():+.2f}R  stop at 0.1 lot = ${stop_usd:.2f}")
rng = np.random.default_rng(7)
for n_tr, lab in ((10, "1 year"), (30, "3 years")):
    ends, ruined = [], 0
    for _ in range(20000):
        eq = 50.0
        for r in rng.choice(R, n_tr):
            if eq < stop_usd: ruined += 1; break      # cannot take the next trade without risking the whole account
            eq += r * stop_usd
        ends.append(eq)
    ends = np.array(ends)
    print(f"{lab:8s}: ruin {ruined/20000:.0%}  median end ${np.median(ends):.0f}  p10 ${np.percentile(ends,10):.0f}  p90 ${np.percentile(ends,90):.0f}")
# same edge sized at 1% of a notional $5k account for comparison
for n_tr, lab in ((30, "3 years"),):
    ends = [5000 * np.prod(1 + 0.01 * rng.choice(R, n_tr)) for _ in range(20000)]
    print(f"notional $5k @1% {lab}: median {np.median(ends)/5000-1:+.1%}  p10 {np.percentile(ends,10)/5000-1:+.1%}  p90 {np.percentile(ends,90)/5000-1:+.1%}")
