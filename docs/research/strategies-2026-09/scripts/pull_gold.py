# READ-ONLY: GOLD history for all research timeframes + specs (copy_rates / symbol_info only).
import time, pickle, MetaTrader5 as mt5, pandas as pd
assert mt5.initialize()
H = {"data": {}, "specs": {}}
for n in ("GOLD",):
    mt5.symbol_select(n, True); i = mt5.symbol_info(n)
    H["specs"][n] = dict(point=i.point, digits=i.digits, spread_pts=i.spread, tick_value=i.trade_tick_value, tick_size=i.trade_tick_size, vmin=i.volume_min,
        vstep=i.volume_step, contract=i.trade_contract_size, bid=None, ask=None, swap_long=i.swap_long, swap_short=i.swap_short, swap_mode=i.swap_mode,
        swap_rollover3days=i.swap_rollover3days)
    for lab, tf in (("M15", mt5.TIMEFRAME_M15), ("H1", mt5.TIMEFRAME_H1), ("H4", mt5.TIMEFRAME_H4), ("D1", mt5.TIMEFRAME_D1)):
        r = None
        for _ in range(6):
            r = mt5.copy_rates_from_pos(n, tf, 1, 99000)
            if r is not None and len(r): break
            time.sleep(2)
        H["data"][(n, lab)] = pd.DataFrame(r); print(n, lab, 0 if r is None else len(r), flush=True)
print("swap", H["specs"]["GOLD"]["swap_long"], H["specs"]["GOLD"]["swap_short"], "rollover3", H["specs"]["GOLD"]["swap_rollover3days"])
mt5.shutdown()
pickle.dump(H, open(r"D:\fxcommand\backend\data\research\history.pkl", "wb"))
