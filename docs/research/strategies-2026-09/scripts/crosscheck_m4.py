"""Milestone 4 parity: the app's backtest with a CostModel vs the research pricing, on real XM GOLD history (read-only)."""
import sys, json, warnings; warnings.filterwarnings("ignore"); sys.path.insert(0, r"D:\fxcommand\backend\data\research")
from prop import *
from robust import SPECS
from fxcommand.learning.candidate import Candidate
from fxcommand.learning.costs import CostModel
from fxcommand.learning.paper import ExitRules, backtest
from fxcommand.learning.objective import r_stats
from fxcommand.strategies import get_strategy
SEAL = int(pd.Timestamp("2025-09-25").timestamp())
sp = SPECS["GOLD"]
def show(label, key, trades, rs):
    res = r_stats([r for x, r in zip(trades, rs) if x.open_time < SEAL]); hold = r_stats([r for x, r in zip(trades, rs) if x.open_time >= SEAL])
    print(json.dumps(dict(run=label, strategy=key, research_n=res.n, research_R=round(res.mean, 4), holdout_n=hold.n, holdout_R=round(hold.mean, 4))))
for tf, key, spread in (("H4", "trend_breakout", 0.57), ("H1", "session_drift", 0.70)):
    df = bars("GOLD", tf).reset_index(drop=True)
    tr = run_prop(df, get_strategy(key).signals(df, {}), spread=spread); show("research pricing", key, tr, swap_prop(tr))
    cm = CostModel(spread=spread, slippage=0.05, swap_long=sp["swap_long"], swap_short=sp["swap_short"], swap_mode=sp["swap_mode"], point=sp["point"], triple_weekday=2, ref_price=PNOW)
    at = backtest(df[["time", "open", "high", "low", "close"]], Candidate.of(key, {}), cm, ExitRules())
    show("app CostModel", key, at, [x.r for x in at])
# the same without swap: any remaining gap would be a code difference
df = bars("GOLD", "H4").reset_index(drop=True)
tr = run_prop(df, get_strategy("trend_breakout").signals(df, {}), spread=0.57); show("research, no swap", "trend_breakout", tr, [x.r for x in tr])
at = backtest(df[["time", "open", "high", "low", "close"]], Candidate.of("trend_breakout", {}), CostModel(spread=0.57, slippage=0.05, ref_price=PNOW), ExitRules())
show("app, no swap", "trend_breakout", at, [x.r for x in at])
from fxcommand.learning.costs import rollover_nights
cal = sum(max(0, x.close_time // 86400 - x.open_time // 86400) for x in at); mt5 = sum(rollover_nights(x.open_time, x.close_time, 2) for x in at)
print(json.dumps(dict(nights_calendar=cal, nights_mt5=mt5, trades=len(at))))
