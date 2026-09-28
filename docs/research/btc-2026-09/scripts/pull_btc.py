"""Read-only: copy BTCUSD history (M15, H1, H4, D1) and its symbol spec from the logged-in MT5 terminal
into backend/data/research/btc.pkl (git-ignored). No orders, no order checks."""
import pickle
import time

import MetaTrader5 as mt5
import pandas as pd

OUT = r"D:\fxcommand\backend\data\research\btc.pkl"
TF = {"M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1}

assert mt5.initialize(), mt5.last_error()
si = mt5.symbol_info("BTCUSD")
tick = mt5.symbol_info_tick("BTCUSD")
data = {}
for name, tf in TF.items():
    r = mt5.copy_rates_from_pos("BTCUSD", tf, 1, 400_000)  # from 1: closed bars only
    df = pd.DataFrame(r)[["time", "open", "high", "low", "close", "tick_volume", "spread"]]
    data[name] = df
    print(name, len(df), pd.Timestamp(df.time.iloc[0], unit="s"), pd.Timestamp(df.time.iloc[-1], unit="s"))
spec = {k: getattr(si, k) for k in ("point", "digits", "trade_contract_size", "volume_min", "volume_step", "swap_mode",
                                     "swap_long", "swap_short", "swap_rollover3days", "trade_stops_level")}
spec["price_now"] = tick.bid
spec["spread_now"] = round(tick.ask - tick.bid, 2)
spec["pulled_at"] = int(time.time())
spec["server_now"] = int(tick.time)
pickle.dump({"data": data, "spec": spec}, open(OUT, "wb"))
print(spec)
mt5.shutdown()
