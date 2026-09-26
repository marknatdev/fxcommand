import sys, warnings; warnings.filterwarnings("ignore"); sys.path.insert(0, r"D:\fxcommand\backend\data\research")
from lab import *
from robust import with_swap
SPLIT = int(pd.Timestamp("2018-01-01").timestamp())
C = Costs(spread=0.57, slippage=0.05)

def ev(df, fr):
    tr = run_frame(df, fr, C); rs = with_swap(tr, "GOLD")
    A = r_stats([r for x, r in zip(tr, rs) if x.open_time < SPLIT]); B = r_stats([r for x, r in zip(tr, rs) if x.open_time >= SPLIT])
    raw = np.mean([x.r for x in tr]) if tr else 0
    yrs = (df.time.iloc[-1] - df.time.iloc[0]) / 3.15e7
    return f"n={len(tr):4d} ({len(tr)/yrs:5.1f}/yr) noswapR={raw:+.3f} | 2010-17 R={A.mean:+.3f} sqn={A.sqn:+.2f} | 2018-26 R={B.mean:+.3f} sqn={B.sqn:+.2f} dd={B.max_dd:.0f}", A.mean, B.mean

def ema_x(df, f, s, both, sl):
    a, b = ind.ema(df.close, f), ind.ema(df.close, s)
    up = (a > b) & (a.shift() <= b.shift()); dn = (a < b) & (a.shift() >= b.shift())
    F = pd.Series(False, index=df.index)
    return frame(df, up, dn if both else F, dn, up, 20, sl, 0)

def turtle(df, n_in, n_out, both, sl):
    up, lo = ind.donchian(df, n_in); xu, xl = ind.donchian(df, n_out); c = df.close
    F = pd.Series(False, index=df.index)
    return frame(df, c > up, (c < lo) if both else F, c < xl, c > xu, 20, sl, 0)

def pdh(df, both, sl, flat_h=22):
    t = pd.to_datetime(df.time, unit="s"); day = t.dt.normalize(); hr = t.dt.hour
    dh = df.groupby(day).high.transform("max"); dl = df.groupby(day).low.transform("min")
    daily = df.groupby(day).agg(h=("high", "max"), l=("low", "min"))
    ph = day.map(daily.h.shift(1)); pl = day.map(daily.l.shift(1))
    c = df.close; ok = (hr >= 2) & (hr < flat_h - 1)
    first_up = ok & (c > ph) & ~((c > ph) & ok).groupby(day).shift(1, fill_value=False).groupby(day).cummax()
    first_dn = ok & (c < pl) & ~((c < pl) & ok).groupby(day).shift(1, fill_value=False).groupby(day).cummax()
    late = hr >= flat_h - 1
    F = pd.Series(False, index=df.index)
    return frame(df, first_up, first_dn if both else F, late, late, 24, sl, 0)

if __name__ == '__main__':
    res = {}
    for tf in ("H4", "D1"):
        df = bars("GOLD", tf).reset_index(drop=True)
        for (f, s) in ((10, 50), (20, 100), (30, 150), (50, 200)):
            for both in (False, True):
                k = f"{tf} EMA{f}/{s} {'L+S' if both else 'L'}"; line, a, b = ev(df, ema_x(df, f, s, both, 3.0)); res[k] = (a, b); print(f"{k:22s} {line}", flush=True)
        for (ni, no) in ((20, 10), (55, 20), (100, 50)):
            for both in (False, True):
                k = f"{tf} Turtle{ni}/{no} {'L+S' if both else 'L'}"; line, a, b = ev(df, turtle(df, ni, no, both, 2.0)); res[k] = (a, b); print(f"{k:22s} {line}", flush=True)
    df = bars("GOLD", "H1").reset_index(drop=True)
    for both in (False, True):
        for sl in (1.0, 2.0):
            k = f"H1 PrevDayBrk {'L+S' if both else 'L'} sl{sl}"; line, a, b = ev(df, pdh(df, both, sl)); res[k] = (a, b); print(f"{k:22s} {line}", flush=True)
