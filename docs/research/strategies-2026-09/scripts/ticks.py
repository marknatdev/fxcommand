# READ-ONLY: GOLD tick spreads around the daily reopen and through the day (last ~40 trading days).
import MetaTrader5 as mt5, pandas as pd, numpy as np, datetime as dt, pickle
assert mt5.initialize()
last = mt5.symbol_info_tick("GOLD").time
rows = []
for d in range(1, 60):
    day0 = (last // 86400 - d) * 86400
    wd = ((day0 // 86400) + 3) % 7
    if wd >= 5: continue
    t = mt5.copy_ticks_range("GOLD", dt.datetime.utcfromtimestamp(day0), dt.datetime.utcfromtimestamp(day0 + 86400), mt5.COPY_TICKS_INFO)
    if t is None or len(t) == 0: continue
    f = pd.DataFrame(t); f["sp"] = f.ask - f.bid; f["m"] = (f.time - day0) // 60
    f["slot"] = (f.m // 15) * 15
    rows.append(f.groupby("slot").sp.median())
mt5.shutdown()
M = pd.concat(rows, axis=1).median(axis=1)
pickle.dump(M, open(r"D:\fxcommand\backend\data\research\gold_spread_by_slot.pkl", "wb"))
print("days:", len(rows))
print(" ".join(f"{int(k)//60:02d}:{int(k)%60:02d}={v:.2f}" for k, v in M.items()))
