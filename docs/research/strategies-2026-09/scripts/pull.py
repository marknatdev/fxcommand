# READ-ONLY: copy_rates only. Saves history to pickles for offline research.
import time, pickle, sys
import MetaTrader5 as mt5, pandas as pd
assert mt5.initialize()
out = sys.argv[1]
syms = ["EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD","USDCHF","NZDUSD","EURJPY","GBPJPY","EURGBP","GOLD","SILVER","OILCash","GER40Cash","US100Cash","US30Cash","US500Cash","UK100Cash","JP225Cash","BTCUSD","ETHUSD"]
tfs = {"M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1}
data, specs = {}, {}
for n in syms:
    mt5.symbol_select(n, True)
for n in syms:
    i = mt5.symbol_info(n); tk = mt5.symbol_info_tick(n)
    specs[n] = dict(point=i.point, digits=i.digits, spread_pts=i.spread, tick_value=i.trade_tick_value, tick_size=i.trade_tick_size,
                    vmin=i.volume_min, vstep=i.volume_step, contract=i.trade_contract_size, bid=tk.bid if tk else None, ask=tk.ask if tk else None,
                    swap_long=i.swap_long, swap_short=i.swap_short, swap_mode=i.swap_mode)
    for lab, tf in tfs.items():
        r = None
        for attempt in range(4):
            r = mt5.copy_rates_from_pos(n, tf, 1, 99000)
            if r is not None and len(r) > 0: break
            time.sleep(1.5)
        df = pd.DataFrame(r) if r is not None else pd.DataFrame()
        data[(n, lab)] = df
        span = f"{pd.to_datetime(df.time.iloc[0], unit='s').date()}..{pd.to_datetime(df.time.iloc[-1], unit='s').date()}" if len(df) else "-"
        print(f"{n:10s} {lab:3s} {len(df):6d} {span}  spread_now={specs[n]['spread_pts']}", flush=True)
mt5.shutdown()
pickle.dump({"data": data, "specs": specs}, open(out, "wb"))
