# READ-ONLY: history + specs for the cheap-at-min-lot index CFDs; Market Watch visibility restored afterwards.
import pickle, time, MetaTrader5 as mt5, pandas as pd
assert mt5.initialize()
syms = ["NETH25Cash","CA60Cash","US2000Cash","US400Cash","CHN50Cash","GerMid50Cash","TaiwanCash","EU50Cash","SWI20Cash","FRA40Cash","SPAIN35Cash","IT40Cash","AUS200Cash","HK50Cash","SING30Cash"]
H = pickle.load(open(r"D:\fxcommand\backend\data\research\history.pkl","rb"))
for n in syms:
    s = mt5.symbol_info(n)
    if s is None: print(n, "missing"); continue
    was = s.visible; mt5.symbol_select(n, True); time.sleep(0.3)
    i = mt5.symbol_info(n)
    H["specs"][n] = dict(point=i.point, digits=i.digits, spread_pts=i.spread, tick_value=i.trade_tick_value, tick_size=i.trade_tick_size, vmin=i.volume_min,
        vstep=i.volume_step, contract=i.trade_contract_size, bid=None, ask=None, swap_long=i.swap_long, swap_short=i.swap_short, swap_mode=i.swap_mode)
    for lab, tf in (("H4", mt5.TIMEFRAME_H4), ("D1", mt5.TIMEFRAME_D1)):
        r = None
        for _ in range(5):
            r = mt5.copy_rates_from_pos(n, tf, 1, 99000)
            if r is not None and len(r): break
            time.sleep(1.5)
        H["data"][(n, lab)] = pd.DataFrame(r) if r is not None else pd.DataFrame()
        print(n, lab, 0 if r is None else len(r), flush=True)
    if not was: mt5.symbol_select(n, False)
mt5.shutdown()
pickle.dump(H, open(r"D:\fxcommand\backend\data\research\history.pkl","wb"))
