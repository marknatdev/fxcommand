import sys; sys.path.insert(0, r"D:\fxcommand\backend\data\research")
from lab import *
from robust import with_swap
from study import sma
df = bars("GOLD", "H1").reset_index(drop=True)
t = pd.to_datetime(df.time, unit="s"); hr = t.dt.hour; wd = t.dt.weekday
d1 = bars("GOLD", "D1"); d1c = d1.set_index(pd.to_datetime(d1.time, unit="s").dt.normalize()).close
trend_ok = (d1c > sma(d1c, 50)).shift(1)  # yesterday's close above its SMA50 (known at today's open)
tok = t.dt.normalize().map(trend_ok).fillna(False).astype(bool)
SPLIT = int(pd.Timestamp("2018-01-01").timestamp())

def asia(entry_open_h, exit_open_h, sl, filt):
    # signal on the bar whose close == entry open -> PaperTrader fills at the next bar's open
    sig_h = (entry_open_h - 1) % 24
    nxt = hr.shift(-1)
    long = (nxt == entry_open_h) & (wd < 5)
    if entry_open_h == 1: long = (hr == 23) & (wd < 4) | (hr == 23) & (wd == 6)  # Sunday bar rarely exists; Mon-Thu 23:00 -> next 01:00
    if filt: long &= tok.shift(-1).fillna(False).astype(bool)
    ex = (nxt == exit_open_h)
    F = pd.Series(False, index=df.index)
    f = frame(df, long, F, ex, None, 24, sl, 0)
    return f

def evaluate(fr, spread):
    tr = run_frame(df, fr, Costs(spread=spread, slippage=0.05))
    rs = with_swap(tr, "GOLD")
    a = [r for x, r in zip(tr, rs) if x.open_time < SPLIT]; b = [r for x, r in zip(tr, rs) if x.open_time >= SPLIT]
    return r_stats(a), r_stats(b)

rows = []
for eh in (1, 2):
    for xh in (4, 5, 6, 9):
        for sl in (1.0, 2.0, 3.0):
            for filt in (False, True):
                A, B = evaluate(asia(eh, xh, sl, filt), 0.70 if eh == 1 else 0.57)
                rows.append(dict(entry=eh, exit=xh, sl=sl, trendD1=filt, nA=A.n, R_A=round(A.mean, 3), sqnA=round(A.sqn, 2), nB=B.n, R_B=round(B.mean, 3), sqnB=round(B.sqn, 2), ddB=round(B.max_dd, 1)))
                print(rows[-1], flush=True)
pd.DataFrame(rows).to_csv(r"D:\fxcommand\backend\data\research\gold_asia.csv", index=False)
