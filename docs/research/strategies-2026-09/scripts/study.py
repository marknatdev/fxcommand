"""Textbook strategy families, fixed params (no tuning => little selection bias), real XM bars, FXCommand PaperTrader costs."""
import sys; sys.path.insert(0, r"D:\fxcommand\backend\data\research")
from lab import *
from fxcommand.learning.paper import ExitRules

def sma(s, n): return s.rolling(n).mean()

def fam_existing(key):
    def f(df):
        return get_strategy(key).signals(df, None)
    return f

def turtle(df):  # Donchian 55 entry / 20 exit, 2 ATR stop, no TP
    up, lo = ind.donchian(df, 55); xu, xl = ind.donchian(df, 20); c = df.close
    return frame(df, c > up, c < lo, c < xl, c > xu, 20, 2.0, 0)

def ema_trend(df):  # EMA50/200 regime, enter on cross, exit on opposite cross, 3 ATR stop
    f, s = ind.ema(df.close, 50), ind.ema(df.close, 200)
    up = (f > s) & (f.shift() <= s.shift()); dn = (f < s) & (f.shift() >= s.shift())
    return frame(df, up, dn, dn, up, 20, 3.0, 0)

def connors(df):  # RSI(2) pullback in SMA200 trend, exit close > SMA5, 3 ATR stop; both sides
    r = ind.rsi(df.close, 2); m200, m5 = sma(df.close, 200), sma(df.close, 5); c = df.close
    return frame(df, (c > m200) & (r < 10), (c < m200) & (r > 90), c > m5, c < m5, 14, 3.0, 0)

def connors_long(df):
    r = ind.rsi(df.close, 2); m200, m5 = sma(df.close, 200), sma(df.close, 5); c = df.close
    F = pd.Series(False, index=df.index)
    return frame(df, (c > m200) & (r < 10), F, c > m5, None, 14, 3.0, 0)

def pullback(df):  # trend pullback: above EMA200, RSI14 crosses back up through 40 -> long (mirror short), 2ATR SL, 3ATR TP
    r = ind.rsi(df.close, 14); e = ind.ema(df.close, 200); c = df.close
    return frame(df, (c > e) & (r.shift() < 40) & (r >= 40), (c < e) & (r.shift() > 60) & (r <= 60), None, None, 14, 2.0, 3.0)

def london_breakout(df):  # M15: range of 03:00-10:00 server time; first close outside range 10:00-13:45 enters; flat by 22:00; stop 1 ATR(M15*?) -> use range-based
    t = pd.to_datetime(df.time, unit="s"); hr = t.dt.hour + t.dt.minute / 60; day = t.dt.date
    inr = (hr >= 3) & (hr < 10)
    hi = df.high.where(inr).groupby(day).transform("max"); lo = df.low.where(inr).groupby(day).transform("min")
    win = (hr >= 10) & (hr < 14)
    c = df.close
    first_up = win & (c > hi) & ~((c > hi) & win).groupby(day).shift(1, fill_value=False).groupby(day).cummax()
    first_dn = win & (c < lo) & ~((c < lo) & win).groupby(day).shift(1, fill_value=False).groupby(day).cummax()
    late = hr >= 22
    f = frame(df, first_up, first_dn, late, late, 14, 1.0, 0)
    rng = (hi - lo)
    f["sl_dist"] = rng; f["tp_dist"] = rng * 1.5
    return f

FAMS = {"ema_cross(def)": fam_existing("ema_cross"), "donchian(def)": fam_existing("donchian_breakout"), "rsi_rev(def)": fam_existing("rsi_reversion"),
        "turtle55/20": turtle, "ema50/200": ema_trend, "connorsRSI2": connors, "connorsRSI2_long": connors_long, "pullback": pullback}

if __name__ == "__main__":
    tfs = sys.argv[1].split(",") if len(sys.argv) > 1 else ["H1","H4","D1"]
    syms = [s for s in SPECS]
    rows = []
    for sym in syms:
        for tf in tfs:
            df = bars(sym, tf)
            if len(df) < 600: continue
            cst = costs_for(sym, df)
            fams = dict(FAMS)
            if tf == "M15": fams["london_brk"] = london_breakout
            for name, fn in fams.items():
                if name == "connorsRSI2_long" and not any(k in sym for k in ("Cash", "GOLD")): continue
                try:
                    tr = run_frame(df, fn(df), cst)
                except Exception as e:
                    print("ERR", sym, tf, name, e); continue
                d = summary(tr, df); d.update(sym=sym, tf=tf, fam=name, bars=len(df),
                    cost_R=round(float(np.median([cst.spread / t_ for t_ in [1]])), 6))
                rows.append(d)
            print(sym, tf, "done", flush=True)
    R = pd.DataFrame(rows)
    R.to_csv(S + r"\study_%s.csv" % "_".join(tfs), index=False)
