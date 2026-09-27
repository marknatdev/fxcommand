"""Parity (read-only, offline): the app's own Evidence path — CostModel.from_symbol with the BTCUSD
profile (recorded-spread floor, swap every night), 2x the typical spread, Trusted History from 2018 —
on the research bars reproduces ledger trial #6 (Turtle 100/50 long H4: 94 trades, +1.07R).

Differences by design: the app prices a fill at the bar's own recorded spread and open price (the
research used the previous bar's), and its slippage is one point, not $5. Tolerance: a few trades,
a few hundredths of R."""
import pickle
import sys
from types import SimpleNamespace

sys.path.insert(0, r"D:\fxcommand\backend")
from fxcommand.learning import evidence as ev  # noqa: E402
from fxcommand.learning.candidate import Candidate  # noqa: E402
from fxcommand.learning.costs import CostModel, trusted_from  # noqa: E402
from fxcommand.learning.paper import ExitRules  # noqa: E402
from fxcommand.learning.research import holdout_from  # noqa: E402

H = pickle.load(open(r"D:\fxcommand\backend\data\research\btc.pkl", "rb"))
spec = H["spec"]
seal = holdout_from(int(spec["server_now"]), "H4")
bars = H["data"]["H4"]
bars = bars[bars.time < seal].rename(columns={"tick_volume": "volume"}).reset_index(drop=True)
info = SimpleNamespace(name="BTCUSD", point=spec["point"], trade_tick_size=spec["point"], trade_tick_value=spec["point"],
                       swap_mode=spec["swap_mode"], swap_long=spec["swap_long"], swap_short=spec["swap_short"],
                       swap_rollover3days=spec["swap_rollover3days"])
costs = CostModel.from_symbol(info, spread=ev.SPREAD_MULTIPLIER * 40.0, ref_price=float(spec["price_now"]), floor_mult=ev.SPREAD_MULTIPLIER)
cand = Candidate.of("trend_breakout", {"entry": 100, "exit": 50, "atr_period": 20, "sl_atr": 2.0, "tp_atr": 0.0, "allow_short": False})
r = ev.evidence_job(bars, cand, costs, ExitRules(), None, None, trusted_from("BTCUSD"))
print({k: r[k] for k in ("trades", "mean_r", "sqn", "avg_nights", "first_ts")}, [(p["label"], p["n"], p["mean"]) for p in r["periods"]])
ok = abs(r["trades"] - 94) <= 4 and abs(r["mean_r"] - 1.07) <= 0.08
print("PARITY", "OK" if ok else "FAILED", "(ledger #6: 94 trades, +1.070R)")
sys.exit(0 if ok else 1)
