"""Pyramiding test for GOLD Trend (Turtle 55/20, long-only), price-scaled costs + swap per unit. Research data only (sealed year excluded)."""
import sys, json, warnings; warnings.filterwarnings("ignore"); sys.path.insert(0, r"D:\fxcommand\backend\data\research")
from prop import *
from robust import swap_price_per_night
SEAL = int(pd.Timestamp("2025-09-25").timestamp()); CUTS = [int(pd.Timestamp(c).timestamp()) for c in ("2018-01-01", "2022-01-01")]
df = bars("GOLD", "H4"); df = df[df.time < SEAL].reset_index(drop=True)
up, _ = ind.donchian(df, 55); _, xl = ind.donchian(df, 20); N = ind.atr(df, 20).to_numpy()
t, o, h, l, c = (df[k].to_numpy() for k in ("time", "open", "high", "low", "close"))
ent_sig = (df.close > up).to_numpy(); ex_sig = (df.close < xl).to_numpy()
swap_n = swap_price_per_night("GOLD", "long")

def sim(max_units, step_n, cap_units_risk=None):
    trades = []; units = []; risk1 = 0; pend_entry = pend_exit = False
    for i in range(len(df)):
        k = c[i - 1] / PNOW if i else 1; sp, sl_ = 0.57 * k, 0.05 * k
        def close_all(px, ts):
            nonlocal units
            tot = 0.0
            for (ep, ot) in units:
                nights = max(0, ts // 86400 - ot // 86400)
                tot += (px - ep) + swap_n * (ep / PNOW) * nights
            trades.append((units[0][1], tot / risk1, len(units))); units = []
        if pend_exit and units: close_all(o[i] - sl_, t[i])
        pend_exit = False
        if pend_entry and not units and N[i - 1] == N[i - 1]:
            ep = o[i] + sp + sl_; units = [(ep, t[i])]; risk1 = 2 * N[i - 1]; stop = ep - 2 * N[i - 1]; last = ep; n_at = N[i - 1]
        pend_entry = False
        if units:
            # adds during the bar (price reaches last entry + step*N)
            while len(units) < max_units and h[i] >= last + step_n * n_at:
                ep = last + step_n * n_at + sp + sl_; units.append((ep, t[i])); last = ep - sp - sl_; stop = max(stop, last - 2 * n_at)
            if l[i] <= stop: close_all(min(stop, o[i]) - sl_, t[i])
        if units and ex_sig[i]: pend_exit = True
        if not units and ent_sig[i] and i > 250: pend_entry = True
    return trades

for mu, st in ((1, 0.5), (2, 0.5), (4, 0.5), (4, 1.0), (2, 1.0)):
    tr = sim(mu, st); rs = np.array([r for _, r, _ in tr]); ot = np.array([x for x, _, _ in tr])
    p = [rs[ot < CUTS[0]], rs[(ot >= CUTS[0]) & (ot < CUTS[1])], rs[ot >= CUTS[1]]]
    s = r_stats(rs)
    # per-unit-risk view: divide by max units used (risk budget comparable to 1 unit)
    print(json.dumps(dict(units=mu, step=st, n=s.n, R_first_unit=round(s.mean, 3), R_per_yr=round(s.total / 15.7, 2), dd=round(s.max_dd, 1),
          p1=round(p[0].mean(), 3), p2=round(p[1].mean(), 3), p3=round(p[2].mean(), 3), avg_units=round(np.mean([u for *_, u in tr]), 2))), flush=True)
