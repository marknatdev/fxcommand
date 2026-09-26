# READ-ONLY screen: which symbols can hold a sensible ATR stop at the MINIMUM lot within a $50 account's risk budget.
# Symbols not already in Market Watch are selected only for the read and hidden again afterwards.
import pickle, numpy as np, pandas as pd, MetaTrader5 as mt5
assert mt5.initialize()
rows = []
for s in mt5.symbols_get():
    if s.trade_mode != 4 or s.trade_tick_size <= 0: continue  # 4 = full access
    was = s.visible
    if not was: mt5.symbol_select(s.name, True)
    r = mt5.copy_rates_from_pos(s.name, mt5.TIMEFRAME_D1, 1, 40)
    i = mt5.symbol_info(s.name)
    if not was: mt5.symbol_select(s.name, False)
    if r is None or len(r) < 20 or i is None: continue
    df = pd.DataFrame(r); pc = df.close.shift()
    tr = np.maximum(df.high - df.low, np.maximum((df.high - pc).abs(), (df.low - pc).abs()))
    atr = float(tr.tail(14).mean()); vpp = i.trade_tick_value / i.trade_tick_size  # account $ per 1.0 price per lot
    spread = i.spread * i.point
    rows.append(dict(sym=s.name, path=s.path.split("\\")[0] + "/" + (s.path.split("\\")[1] if "\\" in s.path else ""), vmin=i.volume_min,
        d1_atr_usd=atr * vpp * i.volume_min, spread_usd=spread * vpp * i.volume_min, spread_atr=spread / atr if atr else np.nan,
        swapL=i.swap_long, swapS=i.swap_short, swap_mode=i.swap_mode))
mt5.shutdown()
R = pd.DataFrame(rows); R.to_csv(r"%s\screen.csv" % r"D:\fxcommand\backend\data\research", index=False)
print(len(R), "tradable symbols with D1 data")
# stop sizes: D1 3xATR, H4 ~ 3x(D1ATR/2.2), H1 ~ 2x(D1ATR/4.5)
R["d1_stop"] = 3 * R.d1_atr_usd; R["h4_stop"] = 3 * R.d1_atr_usd / 2.2; R["h1_stop"] = 2 * R.d1_atr_usd / 4.5
for tf in ("d1_stop", "h4_stop", "h1_stop"):
    for pct in (1, 2, 3, 5):
        ok = R[(R[tf] <= 50 * pct / 100) & (R.spread_atr < 0.1)]
        print(f"{tf} at {pct}% (${50*pct/100:.2f}): {len(ok):4d} symbols  by class: {ok.path.str.split('/').str[0].value_counts().to_dict()}")
print(R[R.path.str.startswith(("Forex", "Spot", "Derivatives"))].sort_values("d1_stop").head(40)[["sym","path","vmin","d1_stop","h4_stop","h1_stop","spread_atr"]].round(3).to_string(index=False))
