"""GOLD scalping, multi-timeframe (M15 context -> M5 setup), research window only (sealed from 2026-06-25). Price-scaled costs, flat before 22:45 (no swap)."""
import sys, json, warnings; warnings.filterwarnings("ignore"); sys.path.insert(0, r"D:\fxcommand\backend\data\research")
from prop import *
SEAL = int(pd.Timestamp("2026-06-25").timestamp())
LEDGER = []
SP, SL = (float(sys.argv[1]), float(sys.argv[2])) if len(sys.argv) > 2 else (0.57, 0.05)
def asof(target_ts, src_close_ts, values):
    a = pd.DataFrame({"t": src_close_ts, "v": values}).sort_values("t")
    b = pd.DataFrame({"t": target_ts, "i": np.arange(len(target_ts))}).sort_values("t")
    return pd.merge_asof(b, a, on="t", direction="backward").sort_values("i")["v"].fillna(False).astype(bool).to_numpy()

m5 = bars("GOLD", "M5"); m5 = m5[m5.time < SEAL].reset_index(drop=True)
m15 = bars("GOLD", "M15"); m15 = m15[m15.time < SEAL].reset_index(drop=True)
m5c = m5.time.to_numpy() + 300; m15c = m15.time.to_numpy() + 900
e20, e50 = ind.ema(m15.close, 20), ind.ema(m15.close, 50)
up15 = pd.Series(asof(m5c, m15c, ((m15.close > e50) & (e20 > e50)).to_numpy()), index=m5.index)
dn15 = pd.Series(asof(m5c, m15c, ((m15.close < e50) & (e20 < e50)).to_numpy()), index=m5.index)
t = pd.to_datetime(m5.time, unit="s"); hm = t.dt.hour * 60 + t.dt.minute; day = t.dt.normalize()
session = (hm >= 10 * 60) & (hm < 21 * 60)
flat = (hm >= 22 * 60 + 40)
T0 = int(m5.time.iloc[0]); T1 = int(m5.time.iloc[-1]); CUTS = [T0 + (T1 - T0) // 3, T0 + 2 * (T1 - T0) // 3]

def score(name, fr, df=m5):
    tr = run_prop(df, fr, spread=SP, slip=SL); rs = [x.r for x in tr]
    P = [r_stats([x.r for x in tr if lo <= x.open_time < hi]) for lo, hi in ((0, CUTS[0]), (CUTS[0], CUTS[1]), (CUTS[1], 2**62))]; s = r_stats(rs)
    days = (df.time.iloc[-1] - df.time.iloc[0]) / 86400 * 5 / 7
    row = dict(name=name, n=s.n, per_day=round(s.n / days, 1), R=round(s.mean, 3), win=round(s.win_rate, 2), dd=round(s.max_dd, 1), p1=round(P[0].mean, 3), p2=round(P[1].mean, 3), p3=round(P[2].mean, 3))
    LEDGER.append(row); print(json.dumps(row), flush=True)

def mk(long, short, exit_l, exit_s, sl, tp, both):
    F = pd.Series(False, index=m5.index)
    return frame(m5, long & session, (short & session) if both else F, exit_l | flat, exit_s | flat, 14, sl, tp)


c = m5.close
def orb2(a_hm, len_min, win_min, tp_mult, use_filter):
    b_hm = a_hm + len_min; end_hm = b_hm + win_min
    inr = (hm >= a_hm) & (hm < b_hm)
    rh = m5.high.where(inr).groupby(day).transform("max"); rl = m5.low.where(inr).groupby(day).transform("min")
    win = (hm >= b_hm) & (hm < end_hm)
    fu = win & (c > rh)
    fu = fu & ~fu.groupby(day).shift(1, fill_value=False).groupby(day).cummax()
    if use_filter: fu = fu & up15
    F = pd.Series(False, index=m5.index)
    ex = (hm >= end_hm + 120) | flat
    f = frame(m5, fu, F, ex, ex, 14, 1.5, 0)
    f["sl_dist"] = (rh - rl).clip(lower=0.5); f["tp_dist"] = (rh - rl) * tp_mult if tp_mult else 0.0
    return f
for L in (15, 30, 45, 60):
    for tp in (1.5, 2.0, 3.0):
        for filt in (True, False):
            score(f"S4n NY ORB L len{L} tp{tp} {'M15' if filt else 'nofilt'}", orb2(990, L, 90, tp, filt))
tag = "xm" if SP >= 0.5 else ("zero" if SP == 0 else "low")
json.dump(LEDGER, open(rf"D:\fxcommand\backend\data\research\ledger_scalp3_{tag}.json", "w"), indent=1)
print("trials", len(LEDGER))
