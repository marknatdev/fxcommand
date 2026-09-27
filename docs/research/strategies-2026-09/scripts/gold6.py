"""GOLD pair upgrades. Research data ends 2025-09-25 (the last 12 months are sealed). Price-scaled costs. Every variant is a counted trial."""
import sys, json, warnings; warnings.filterwarnings("ignore"); sys.path.insert(0, r"D:\fxcommand\backend\data\research")
from prop import *
from study import sma
SEAL = int(pd.Timestamp("2025-09-25").timestamp())
CUTS = ("2018-01-01", "2022-01-01")
LEDGER = []

def research(tf):
    df = bars("GOLD", tf); return df[df.time < SEAL].reset_index(drop=True)

def score(name, df, fr, spread=0.57):
    tr = run_prop(df, fr, spread=spread); rs = swap_prop(tr); P = periods(tr, rs, CUTS); s = r_stats(rs)
    yrs = (df.time.iloc[-1] - df.time.iloc[0]) / 3.15e7
    row = dict(name=name, n=s.n, per_yr=round(s.n / yrs, 1), R=round(s.mean, 3), R_per_yr=round(s.total / yrs, 2), win=round(s.win_rate, 2), dd=round(s.max_dd, 1),
               p1=round(P[0].mean, 3), p2=round(P[1].mean, 3), p3=round(P[2].mean, 3))
    LEDGER.append(row); print(json.dumps(row), flush=True); return row

def asof(target_close_ts, src_close_ts, values):
    """Value of a series known at each target time (last source bar closed at or before it)."""
    a = pd.DataFrame({"t": src_close_ts, "v": values}).sort_values("t")
    b = pd.DataFrame({"t": target_close_ts, "i": np.arange(len(target_close_ts))}).sort_values("t")
    m = pd.merge_asof(b, a, on="t", direction="backward").sort_values("i")
    return m["v"].fillna(False).astype(bool).to_numpy()

def state_long(entry, exit_):
    e, x = entry.to_numpy(), exit_.to_numpy(); st = np.zeros(len(e), dtype=bool); on = False
    for i in range(len(e)):
        if on and x[i]: on = False
        elif not on and e[i]: on = True
        st[i] = on
    return pd.Series(st, index=entry.index)

def turtle_sys(df, ni, no):
    up, _ = ind.donchian(df, ni); _, xl = ind.donchian(df, no); c = df.close
    return c > up, c < xl

def trend_frame(df, long, exit_long, sl=2.0):
    F = pd.Series(False, index=df.index); return frame(df, long, F, exit_long, None, 20, sl, 0)

d1 = research("D1"); d1_close_t = d1.time.to_numpy() + 86400

# ---------------- GOLD Trend (H4) ----------------
h4 = research("H4"); h4_close_t = h4.time.to_numpy() + 4 * 3600
e, x = turtle_sys(h4, 55, 20)
score("T0 turtle55/20 L (baseline)", h4, trend_frame(h4, e, x))
sts = [state_long(*turtle_sys(h4, a, b)) for a, b in ((20, 10), (55, 20), (100, 50))]
vote = sum(s.astype(int) for s in sts)
score("T1 ensemble 2-of-3", h4, trend_frame(h4, (vote >= 2) & (vote.shift() < 2), vote < 2))
score("T1b ensemble any-of-3", h4, trend_frame(h4, (vote >= 1) & (vote.shift() < 1), vote < 1))
reg200 = asof(h4_close_t, d1_close_t, (d1.close > sma(d1.close, 200)).to_numpy())
reg50 = asof(h4_close_t, d1_close_t, (ind.ema(d1.close, 50) > ind.ema(d1.close, 200)).to_numpy())
score("T2 turtle + D1 close>SMA200", h4, trend_frame(h4, e & reg200, x))
score("T2b turtle + D1 EMA50>EMA200", h4, trend_frame(h4, e & reg50, x))
score("T3 turtle + H4 ADX>20", h4, trend_frame(h4, e & (ind.adx(h4, 14) > 20), x))
score("T3b ensemble2 + D1 close>SMA200", h4, trend_frame(h4, (vote >= 2) & (vote.shift() < 2) & reg200, vote < 2))
for sl in (1.5, 2.5, 3.0): score(f"T0 sl{sl}", h4, trend_frame(h4, e, x, sl))

# ---------------- GOLD Reopen Drift (H1) ----------------
h1 = research("H1"); h1_close_t = h1.time.to_numpy() + 3600
t = pd.to_datetime(h1.time, unit="s"); m = t.dt.hour * 60 + t.dt.minute; wd = t.dt.weekday
nxt = m.shift(-1); nwd = wd.shift(-1)
def reopen(filt=None, exit_h=4, sl=1.5, days=(0, 1, 2, 3, 4)):
    long = (nxt == 60) & nwd.isin(days)
    if filt is not None: long &= pd.Series(filt, index=h1.index)
    F = pd.Series(False, index=h1.index)
    return frame(h1, long, F, nxt == exit_h * 60, None, 24, sl, 0)
score("R0 reopen 01->04 sl1.5 (baseline)", h1, reopen(), spread=0.70)
score("R0b reopen 01->05 sl1.5", h1, reopen(exit_h=5), spread=0.70)
h4_up = asof(h1_close_t, h4_close_t, (h4.close > ind.ema(h4.close, 50)).to_numpy())
d1_up = asof(h1_close_t, d1_close_t, (d1.close > sma(d1.close, 50)).to_numpy())
d1_green = asof(h1_close_t, d1_close_t, (d1.close > d1.open).to_numpy())
score("R1 reopen + H4 close>EMA50", h1, reopen(h4_up), spread=0.70)
score("R1b reopen + H4 close<EMA50", h1, reopen(~h4_up), spread=0.70)
score("R2 reopen + D1 close>SMA50", h1, reopen(d1_up), spread=0.70)
score("R3 reopen Tue-Fri", h1, reopen(days=(1, 2, 3, 4)), spread=0.70)
score("R4 reopen after down day", h1, reopen(~d1_green), spread=0.70)
score("R4b reopen after up day", h1, reopen(d1_green), spread=0.70)
json.dump(LEDGER, open(r"D:\fxcommand\backend\data\research\ledger_gold6.json", "w"), indent=1)
print("trials:", len(LEDGER))
