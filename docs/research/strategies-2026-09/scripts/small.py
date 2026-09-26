import sys; sys.path.insert(0, r"D:\fxcommand\backend\data\research")
from lab import *
from robust import connors_p, with_swap
syms = ["NETH25Cash","CA60Cash","US2000Cash","US400Cash","CHN50Cash","GerMid50Cash","TaiwanCash","EU50Cash","SWI20Cash","FRA40Cash","IT40Cash","AUS200Cash","HK50Cash","US500Cash","GER40Cash","JP225Cash","US100Cash","US30Cash"]
for sym in syms:
    df = bars(sym, "D1")
    if len(df) < 400: print(sym, "short history", len(df)); continue
    sp = SPECS[sym]; vpp = sp["tick_value"] / sp["tick_size"]
    atr_now = float(ind.atr(df, 14).iloc[-1])
    line = f"{sym:12s} yrs={len(df)/252:4.1f}"
    for sl in (1.5, 2.0, 3.0):
        tr = run_frame(df, connors_p(10, 200, 5, True, sl)(df), costs_for(sym, df, 2))
        rs = with_swap(tr, sym); s = r_stats(rs)
        mid = int(df.time.iloc[len(df)//2]); h1 = np.mean([r for t, r in zip(tr, rs) if t.open_time < mid] or [0]); h2 = np.mean([r for t, r in zip(tr, rs) if t.open_time >= mid] or [0])
        risk_pct = sl * atr_now * vpp * sp["vmin"] / 50 * 100
        line += f" | sl{sl}: n={s.n} R={s.mean:+.3f} sqn={s.sqn:+.2f} halves={h1:+.2f}/{h2:+.2f} minlot_risk={risk_pct:.0f}%"
    print(line, flush=True)
