"""Checks before the holdout: (1) the app's own trend_breakout (entry=100, exit=50, long-only) gives
the same trades as ledger #6; (2) historical ticks confirm the recorded bar spread (read-only)."""
import MetaTrader5 as mt5

from btc_lab import *  # noqa: F403
from fxcommand.strategies import get_strategy

strat = get_strategy("trend_breakout")
p = strat.resolve({"entry": 100, "exit": 50, "atr_period": 20, "sl_atr": 2.0, "tp_atr": 0.0, "allow_short": False})
h4 = bars("H4")
sig = strat.signals(h4, p)
fr = pd.DataFrame({k: sig[k] for k in ("long", "short", "exit_long", "exit_short", "sl_dist", "tp_dist")})
fr["info_atr"] = ind.atr(h4, 20)
fr.loc[h4.time < START, ["long", "short"]] = False
s, per, nights = stats(run(h4, fr))
print(f"app trend_breakout 100/50: n={s.n} R={s.mean:.3f} sqn={s.sqn:.2f} p=({per[0].mean:.3f}, {per[1].mean:.3f}, {per[2].mean:.3f})  [ledger #6: n=94 R=1.07]")

assert mt5.initialize(), mt5.last_error()
for day in ("2019-03-12", "2020-03-10", "2021-03-09", "2022-03-08", "2024-03-12"):
    a = int(pd.Timestamp(day + " 14:00").timestamp())
    tk = mt5.copy_ticks_range("BTCUSD", a, a + 4 * 3600, mt5.COPY_TICKS_INFO)
    r = mt5.copy_rates_range("BTCUSD", mt5.TIMEFRAME_H1, a, a + 4 * 3600)
    if tk is None or not len(tk):
        print(day, "no ticks", mt5.last_error(), " bar spread (points):", list(pd.DataFrame(r).spread) if r is not None else None)
        continue
    tk = pd.DataFrame(tk)
    sp = tk.ask - tk.bid
    print(f"{day}: {len(tk)} ticks, tick spread $ p50/p90 {sp.median():.2f}/{sp.quantile(.9):.2f}, price {tk.bid.median():.0f}; bar spread $: {[x * POINT for x in pd.DataFrame(r).spread]}")
mt5.shutdown()
