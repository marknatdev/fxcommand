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
# S1 MTF pullback: M15 trend + M5 RSI(5) hook out of the extreme
r = ind.rsi(c, 5)
for both in (False, True):
    L = up15 & (r.shift() < 30) & (r >= 30); S = dn15 & (r.shift() > 70) & (r <= 70)
    score(f"S1 pullback RSI5 {'L+S' if both else 'L'} sl1.5 tp2", mk(L, S, r > 70, r < 30, 1.5, 2.0, both))
# S2 MTF breakout: M15 trend + M5 12-bar high/low break, exit on 6-bar opposite channel
hi12, lo12 = ind.donchian(m5, 12); hi6, lo6 = ind.donchian(m5, 6)
for both in (False, True):
    score(f"S2 breakout12 {'L+S' if both else 'L'} sl1.5", mk(up15 & (c > hi12), dn15 & (c < lo12), c < lo6, c > hi6, 1.5, 0, both))
# S3/S4 opening-range breakouts with M15 trend: London 10:00-10:30, NY 16:30-17:00 (server time)
def orb(a_hm, b_hm, end_hm, both):
    inr = (hm >= a_hm) & (hm < b_hm)
    rh = m5.high.where(inr).groupby(day).transform("max"); rl = m5.low.where(inr).groupby(day).transform("min")
    win = (hm >= b_hm) & (hm < end_hm)
    fu = win & (c > rh); fd = win & (c < rl)
    fu = fu & ~fu.groupby(day).shift(1, fill_value=False).groupby(day).cummax()
    fd = fd & ~fd.groupby(day).shift(1, fill_value=False).groupby(day).cummax()
    F = pd.Series(False, index=m5.index)
    ex = (hm >= end_hm + 120) | flat
    f = frame(m5, fu & up15, (fd & dn15) if both else F, ex, ex, 14, 1.5, 0)
    f["sl_dist"] = (rh - rl).clip(lower=0.5); f["tp_dist"] = (rh - rl) * 2
    return f
for both in (False, True):
    score(f"S3 London ORB {'L+S' if both else 'L'}", orb(600, 630, 720, both))
    score(f"S4 NY ORB {'L+S' if both else 'L'}", orb(990, 1020, 1110, both))
# S5 mean reversion in M15 range: ADX(M15)<20, M5 close back inside lower/upper Bollinger band, exit at mid
adx15 = pd.Series(asof(m5c, m15c, (ind.adx(m15, 14) < 20).to_numpy()), index=m5.index)
mid = c.rolling(20).mean(); sd = c.rolling(20).std(); lb, ub = mid - 2 * sd, mid + 2 * sd
for both in (False, True):
    L = adx15 & (c.shift() < lb.shift()) & (c >= lb); S = adx15 & (c.shift() > ub.shift()) & (c <= ub)
    score(f"S5 BB revert {'L+S' if both else 'L'} sl1.5", mk(L, S, c >= mid, c <= mid, 1.5, 0, both))
json.dump(LEDGER, open(r"D:\fxcommand\backend\data\research\ledger_scalp1.json", "w"), indent=1)

# ---------------- M1 trigger: M15 context -> M5 setup -> M1 entry ----------------
m1 = bars("GOLD", "M1"); m1 = m1[(m1.time < SEAL) & (m1.time >= m5.time.iloc[0])].reset_index(drop=True)
m1c = m1.time.to_numpy() + 60
t1 = pd.to_datetime(m1.time, unit="s"); hm1 = t1.dt.hour * 60 + t1.dt.minute
sess1 = (hm1 >= 600) & (hm1 < 1260); flat1 = hm1 >= 1360
atr5 = ind.atr(m5, 14)
def m5_to_m1(values, hold_min=5):
    """An M5 condition true at an M5 close stays armed on M1 for hold_min minutes."""
    v = pd.Series(values.to_numpy(), index=m5c)
    armed = np.zeros(len(m1), dtype=bool)
    idx = np.searchsorted(m1c, v.index[v.to_numpy().astype(bool)])
    for i in idx:
        armed[i:i + hold_min] = True
    return pd.Series(armed, index=m1.index)
def m5_value(series):
    a = pd.DataFrame({"t": m5c, "v": series.to_numpy()}); b = pd.DataFrame({"t": m1c, "i": np.arange(len(m1))})
    return pd.merge_asof(b, a, on="t", direction="backward").sort_values("i")["v"].to_numpy()
def m1_frame(setup_l, setup_s, exit_l, exit_s, both):
    armed_l, armed_s = m5_to_m1(setup_l), m5_to_m1(setup_s)
    trig_l = armed_l & (m1.close > m1.high.shift()); trig_s = armed_s & (m1.close < m1.low.shift())
    F = pd.Series(False, index=m1.index)
    f = frame(m1, trig_l & sess1, (trig_s & sess1) if both else F, pd.Series(m5_value(exit_l).astype(bool), index=m1.index) | flat1, pd.Series(m5_value(exit_s).astype(bool), index=m1.index) | flat1, 14, 1.5, 0)
    a5 = m5_value(atr5); f["sl_dist"] = 1.5 * a5; f["tp_dist"] = 2.0 * a5; f["info_atr"] = a5
    return f
r5 = ind.rsi(c, 5)
L1 = up15 & (r5.shift() < 30) & (r5 >= 30); S1_ = dn15 & (r5.shift() > 70) & (r5 <= 70)
score("S1m1 pullback + M1 trigger L+S", m1_frame(L1, S1_, r5 > 70, r5 < 30, True), df=m1)
L5 = adx15 & (c.shift() < lb.shift()) & (c >= lb)
score("S5m1 BB revert + M1 trigger L", m1_frame(L5, pd.Series(False, index=m5.index), c >= mid, c <= mid, False), df=m1)
tag = "xm" if SP >= 0.5 else ("zero" if SP == 0 else "low")
json.dump(LEDGER, open(rf"D:\fxcommand\backend\data\research\ledger_scalp2_{tag}.json", "w"), indent=1)

print("trials", len(LEDGER), "window", pd.to_datetime(m5.time.iloc[0], unit="s").date(), "->", pd.to_datetime(m5.time.iloc[-1], unit="s").date())
