"""Score the one BTC finalist on the sealed holdout, once (rules: ../HOLDOUT-RULES.md)."""
import os

import btc_lab as L
from btc_lab import *  # noqa: F403
from fxcommand.strategies import get_strategy

OUT = r"D:\fxcommand\docs\research\btc-2026-09\results\holdout_btc_2026-09-27.jsonl"
assert not os.path.exists(OUT), "the holdout was already scored: the result is final"

df = L.H["data"]["H4"]
first = int(np.searchsorted(df.time.to_numpy(), L.SEAL))
df = df.iloc[first - 600:].reset_index(drop=True)  # warm-up from research bars, then the holdout
strat = get_strategy("trend_breakout")
p = strat.resolve({"entry": 100, "exit": 50, "atr_period": 20, "sl_atr": 2.0, "tp_atr": 0.0, "allow_short": False})
sig = strat.signals(df, p)
fr = pd.DataFrame({k: sig[k] for k in ("long", "short", "exit_long", "exit_short", "sl_dist", "tp_dist")})
fr["info_atr"] = ind.atr(df, 20)
L.START = L.SEAL  # only Signals from the seal on count (run() filters on START)
tr = run(df, fr)
tr = [x for x in tr if x.signal_time >= L.SEAL]
s = r_stats([x.r for x in tr])
row = {"finalist": "BTC Trend trend_breakout 100/50 L sl2 H4", "from": str(pd.Timestamp(L.SEAL, unit="s")), "to": str(pd.Timestamp(int(df.time.iloc[-1]), unit="s")),
       "n": s.n, "R": round(s.mean, 3), "total_R": round(s.total, 2), "win": round(s.win_rate, 2), "dd": round(s.max_dd, 1),
       "passed": bool(s.n >= 5 and s.mean > 0),
       "trades": [{"signal": str(pd.Timestamp(x.signal_time, unit="s")), "close": str(pd.Timestamp(x.close_time, unit="s")), "r": round(x.r, 3), "reason": x.reason} for x in tr]}
with open(OUT, "w") as f:
    f.write(json.dumps(row) + "\n")
print(json.dumps(row, indent=1))
