"""ONE-TIME evaluation of finalists on the sealed year (2025-09-25 .. now). Warm-up uses earlier bars; only trades opened in the sealed year count."""
import sys, json, warnings; warnings.filterwarnings("ignore"); sys.path.insert(0, r"D:\fxcommand\backend\data\research")
from prop import *
from gold6 import asof, turtle_sys, trend_frame  # noqa (gold6 re-runs its research block on import; acceptable)
SEAL = int(pd.Timestamp("2025-09-25").timestamp())
def hold(name, df, fr, spread=0.57):
    tr = run_prop(df, fr, spread=spread); rs = swap_prop(tr)
    sel = [r for x, r in zip(tr, rs) if x.open_time >= SEAL]; s = r_stats(sel)
    print(json.dumps(dict(finalist=name, n=s.n, R=round(s.mean, 3), total_R=round(s.total, 2), win=round(s.win_rate, 2), dd=round(s.max_dd, 1))), flush=True)
h4 = bars("GOLD", "H4").reset_index(drop=True); e, x = turtle_sys(h4, 55, 20)
hold("GOLD Trend turtle55/20 L sl2", h4, trend_frame(h4, e, x))
h1 = bars("GOLD", "H1").reset_index(drop=True); h4c = h4.time.to_numpy() + 14400; h1c = h1.time.to_numpy() + 3600
t = pd.to_datetime(h1.time, unit="s"); m = t.dt.hour * 60 + t.dt.minute; nxt = m.shift(-1); nwd = t.dt.weekday.shift(-1)
def reopen(filt=None, exit_h=4, sl=1.5):
    long = (nxt == 60) & nwd.isin((0, 1, 2, 3, 4))
    if filt is not None: long &= pd.Series(filt, index=h1.index)
    return frame(h1, long, pd.Series(False, index=h1.index), nxt == exit_h * 60, None, 24, sl, 0)
below = ~asof(h1c, h4c, (h4.close > ind.ema(h4.close, 50)).to_numpy())
hold("Reopen 01->04 (baseline)", h1, reopen(), 0.70)
hold("Reopen 01->05", h1, reopen(exit_h=5), 0.70)
hold("Reopen dip (H4<EMA50) 01->05", h1, reopen(below, 5), 0.70)
