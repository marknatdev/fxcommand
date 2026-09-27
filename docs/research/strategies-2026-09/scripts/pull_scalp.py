# READ-ONLY: GOLD M1/M5 history in date chunks (copy_rates_range only).
import time, pickle, datetime as dt, MetaTrader5 as mt5, pandas as pd
assert mt5.initialize()
H = pickle.load(open(r"D:\fxcommand\backend\data\research\history.pkl", "rb"))
for lab, tf, days in (("M5", mt5.TIMEFRAME_M5, 30), ("M1", mt5.TIMEFRAME_M1, 7)):
    frames, end = [], dt.datetime(2026, 9, 27)
    start_all = dt.datetime(2020, 1, 1)
    cur = start_all
    while cur < end:
        nxt = min(cur + dt.timedelta(days=days), end)
        r = None
        for _ in range(3):
            r = mt5.copy_rates_range("GOLD", tf, cur, nxt)
            if r is not None: break
            time.sleep(1)
        if r is not None and len(r): frames.append(pd.DataFrame(r))
        cur = nxt
    df = pd.concat(frames).drop_duplicates("time").sort_values("time").reset_index(drop=True) if frames else pd.DataFrame()
    df = df.iloc[:-1] if len(df) else df  # drop the forming bar
    H["data"][("GOLD", lab)] = df
    print(lab, len(df), pd.to_datetime(df.time.iloc[0], unit="s") if len(df) else "-", "->", pd.to_datetime(df.time.iloc[-1], unit="s") if len(df) else "-", flush=True)
mt5.shutdown()
pickle.dump(H, open(r"D:\fxcommand\backend\data\research\history.pkl", "wb"))
