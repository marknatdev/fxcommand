import sys; sys.path.insert(0, r"D:\fxcommand\backend\data\research")
from lab import *
from study import sma, ema_trend, turtle, pullback

def swap_price_per_night(sym, side):
    sp = SPECS[sym]; sw = sp["swap_long"] if side == "long" else sp["swap_short"]
    if sp["swap_mode"] == 1: return sw * sp["point"]                       # points
    if sp["swap_mode"] in (2, 3, 4): return sw / (sp["tick_value"] / sp["tick_size"])  # money per lot -> price
    return 0.0

def with_swap(trades, sym):
    out = []
    for t in trades:
        if t.r == 0: out.append(0.0); continue
        risk = abs(t.exit - t.entry) / abs(t.r)
        nights = max(0, (t.close_time // 86400) - (t.open_time // 86400))  # calendar midnights ~ weekend triple
        out.append(t.r + swap_price_per_night(sym, t.side) * nights / risk)  # swap is signed (negative = cost)
    return out

def connors_p(rsi_th=10, trend=200, exit_ma=5, long_only=True, sl=3.0):
    def f(df):
        r = ind.rsi(df.close, 2); c = df.close; m, x = sma(c, trend), sma(c, exit_ma)
        F = pd.Series(False, index=df.index)
        sh = F if long_only else (c < m) & (r > 100 - rsi_th)
        return frame(df, (c > m) & (r < rsi_th), sh, c > x, c < x, 14, sl, 0)
    return f

def st(rs, df):
    s = r_stats(rs); return f"n={s.n:4d} R={s.mean:+.3f} sqn={s.sqn:+.2f} dd={s.max_dd:.1f}"

mode = sys.argv[1] if __name__ == "__main__" else ""
if mode == "survivors":
    todo = [(s, "D1", "connorsL", connors_p()) for s in ("US500Cash","US100Cash","US30Cash","JP225Cash","GER40Cash","UK100Cash","GOLD")]
    todo += [("BTCUSD","H4","ema50/200",ema_trend),("GOLD","H4","ema50/200",ema_trend),("US100Cash","H4","ema50/200",ema_trend),("OILCash","D1","turtle",turtle),
             ("US500Cash","D1","pullback",pullback),("US100Cash","H4","pullback",pullback),("EURUSD","H4","ema50/200",ema_trend),("BTCUSD","D1","turtle",turtle)]
    for sym, tf, name, fn in todo:
        df = bars(sym, tf); fr = fn(df)
        line = f"{sym:10s} {tf} {name:10s}"
        for m in (1, 2, 3):
            tr = run_frame(df, fr, costs_for(sym, df, m))
            line += f" | x{m} {st([t.r for t in tr], df)} +swap R={np.mean(with_swap(tr, sym)):+.3f}"
        print(line, flush=True)
elif mode == "neigh":
    for sym in ("US500Cash","US100Cash","US30Cash","GER40Cash","JP225Cash"):
        df = bars(sym, "D1"); cst = costs_for(sym, df, 2); cells = []
        for th in (5, 10, 15, 20):
            for tm in (150, 200, 250):
                for xm in (3, 5, 10):
                    tr = run_frame(df, connors_p(th, tm, xm)(df), cst)
                    cells.append(np.mean(with_swap(tr, sym)) if tr else np.nan)
        a = np.array(cells); print(f"{sym:10s} 36 neighbours (2x spread + swap): positive {np.mean(a>0):.0%}, median R {np.nanmedian(a):+.3f}, min {np.nanmin(a):+.3f}, max {np.nanmax(a):+.3f}", flush=True)
