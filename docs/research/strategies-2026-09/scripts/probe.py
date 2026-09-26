# READ-ONLY probe: symbol names, specs, history depth. No order functions used.
import MetaTrader5 as mt5
assert mt5.initialize()
t = mt5.terminal_info(); print("maxbars", t.maxbars, "connected", t.connected)
want = ["EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD","USDCHF","NZDUSD","EURJPY","GBPJPY","EURGBP","GOLD","SILVER"]
names = [s.name for s in mt5.symbols_get()]
extra = [n for n in names if any(k in n.upper() for k in ("US30","US100","US500","GER40","UK100","JP225","BTC","ETH","OIL","NAS"))]
print("extra:", extra[:40])
for n in want + extra[:12]:
    if n not in names: print(n, "missing"); continue
    mt5.symbol_select(n, True)
    i = mt5.symbol_info(n)
    for tf, lab in ((mt5.TIMEFRAME_M15,"M15"),(mt5.TIMEFRAME_H1,"H1"),(mt5.TIMEFRAME_D1,"D1")):
        r = mt5.copy_rates_from_pos(n, tf, 1, 200000)
        nb = 0 if r is None else len(r)
        print(f"{n:12s} {lab} bars={nb}", end="  ")
    print(f"spread={i.spread} point={i.point} digits={i.digits} tickval={i.trade_tick_value:.4f} vmin={i.volume_min} calc={i.trade_calc_mode}")
mt5.shutdown()
