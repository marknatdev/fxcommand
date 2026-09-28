"""BTCUSD round 1: five pre-registered families, 24 variants, every one a counted trial
(spec-btc-strategies D8). Variants were written down before any BTC result was seen."""
from btc_lab import *  # noqa: F403

OUT = r"D:\fxcommand\docs\research\btc-2026-09\results\ledger_round1.json"
F = None


def false(df):
    return pd.Series(False, index=df.index)


# ---------- 1. Turtle breakout (H4, H1; long and short separately) ----------
def turtle(df, ni, no, side, sl=2.0):
    up, lo = ind.donchian(df, ni)
    xu, xl = ind.donchian(df, no)
    c = df.close
    f = false(df)
    if side == "L":
        return frame(df, c > up, f, c < xl, None, sl, atr_period=20)
    return frame(df, f, c < lo, None, c > xu, sl, atr_period=20)


h4, h1 = bars("H4"), bars("H1")
score("turtle 55/20 long", "1 turtle", h4, turtle(h4, 55, 20, "L"), "H4")
score("turtle 55/20 short", "1 turtle", h4, turtle(h4, 55, 20, "S"), "H4")
score("turtle 55/20 long", "1 turtle", h1, turtle(h1, 55, 20, "L"), "H1")
score("turtle 55/20 short", "1 turtle", h1, turtle(h1, 55, 20, "S"), "H1")
score("turtle 20/10 long", "1 turtle", h4, turtle(h4, 20, 10, "L"), "H4")
score("turtle 100/50 long", "1 turtle", h4, turtle(h4, 100, 50, "L"), "H4")

# ---------- 2. Time of week (H1) ----------
hr, wd = hour(h1), weekday(h1)
weekdays = wd <= 4


def hold(entry_bar, exit_bar, side, sl=1.5):
    f = false(h1)
    if side == "L":
        return frame(h1, entry_bar, f, exit_bar, None, sl, atr_period=24)
    return frame(h1, f, entry_bar, None, exit_bar, sl, atr_period=24)


wk_in, wk_out = (wd == 4) & (hr == 22), (wd == 0) & (hr == 1)  # fill Fri 23:00 -> Mon 02:00
score("weekend hold long Fri23->Mon02", "2 time", h1, hold(wk_in, wk_out, "L", 2.0), "H1")
score("weekend hold short Fri23->Mon02", "2 time", h1, hold(wk_in, wk_out, "S", 2.0), "H1")
us_in, us_out = weekdays & (hr == 15), hr == 21  # fill 16:00 -> 22:00 (NY session, no rollover)
score("US session long 16->22", "2 time", h1, hold(us_in, us_out, "L"), "H1")
score("US session short 16->22", "2 time", h1, hold(us_in, us_out, "S"), "H1")
as_in, as_out = hr == 0, hr == 7  # fill 01:00 -> 08:00 (Asia, no rollover)
score("Asia long 01->08", "2 time", h1, hold(as_in, as_out, "L"), "H1")
score("Asia short 01->08", "2 time", h1, hold(as_in, as_out, "S"), "H1")

# ---------- 3. US-open range breakout (H1) ----------
day = h1.time // DAY
rng = h1[hr.isin((15, 16))].groupby(day[hr.isin((15, 16))]).agg(hi=("high", "max"), lo=("low", "min"))
rhi, rlo = day.map(rng.hi), day.map(rng.lo)
window = weekdays & hr.between(17, 21)
up_b, dn_b = window & (h1.close > rhi), window & (h1.close < rlo)
first = (up_b | dn_b).astype(int).groupby(day).cumsum().eq(1) & (up_b | dn_b)
up_b, dn_b = up_b & first, dn_b & first
a24 = ind.atr(h1, 24)
stop = np.maximum(rhi - rlo, 0.5 * a24)
ex = hr == 22  # fill 23:00, before the rollover
for name, L, S, tp in (("both", True, True, None), ("long", True, False, None), ("short", False, True, None), ("both tp 2x", True, True, 2.0)):
    fr = frame(h1, up_b if L else false(h1), dn_b if S else false(h1), ex, ex, atr_period=24, sl=stop, tp=(stop * tp if tp else stop * 0))
    score(f"US-open breakout {name}", "3 us-open", h1, fr, "H1")

# ---------- 4. Trend pullback (H4) ----------
e200, r14 = ind.ema(h4.close, 200), ind.rsi(h4.close, 14)
pl = (h4.close > e200) & (r14 > 40) & (r14.shift() <= 40)
ps = (h4.close < e200) & (r14 < 60) & (r14.shift() >= 60)
f4 = false(h4)
score("pullback long sl2 tp3", "4 pullback", h4, frame(h4, pl, f4, None, None, 2.0, 3.0), "H4")
score("pullback short sl2 tp3", "4 pullback", h4, frame(h4, f4, ps, None, None, 2.0, 3.0), "H4")
score("pullback both sl2 tp3", "4 pullback", h4, frame(h4, pl, ps, None, None, 2.0, 3.0), "H4")
score("pullback long sl2 tp2", "4 pullback", h4, frame(h4, pl, f4, None, None, 2.0, 2.0), "H4")

# ---------- 5. Intraday momentum, flat before the rollover (H1) ----------
d1 = bars("D1")
d1_atr = pd.Series(ind.atr(d1, 14).to_numpy(), index=d1.time // DAY + 1)  # known from the next day on
datr = day.map(d1_atr)
dopen = day.map(h1.groupby(day).open.first())
move = h1.close - dopen
for name, thr, L, S in (("both 0.5", 0.5, True, True), ("long 0.5", 0.5, True, False), ("short 0.5", 0.5, False, True), ("both 0.25", 0.25, True, True)):
    at = weekdays & (hr == 11)  # bar closing 12:00; fill 12:00 -> exit fill 22:00
    fr = frame(h1, (at & (move > thr * datr)) if L else false(h1), (at & (move < -thr * datr)) if S else false(h1), hr == 21, hr == 21, 1.5, atr_period=24)
    score(f"midday momentum {name}", "5 momentum", h1, fr, "H1")

save(OUT)
n = len(LEDGER)
print(f"trials {n}, deflation {deflation(n):.2f}")
for r in LEDGER:
    if passes(r, n):
        print("PASSES (before neighbours):", r["id"], r["name"], r["tf"])
