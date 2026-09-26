"""Pin the GOLD strategies' per-bar signals as a golden fixture. Regenerate only on a deliberate
behaviour change: uv run python tests/make_golden_gold.py"""

import json
from pathlib import Path

from fxcommand.broker.sim import SimBroker
from fxcommand.broker.types import Timeframe
from fxcommand.strategies import STRATEGIES

MON_08 = 1_704_700_800
PARAM_SETS = {
    "trend_breakout": [{}, {"entry": 20, "exit": 8, "allow_short": True}],
    "session_drift": [{}, {"exit_hour": 5, "sl_atr": 1.0, "max_gap_min": 0}],
}
SERIES = (("GOLD", Timeframe.H1), ("GOLD", Timeframe.H4))


def series():
    sim = SimBroker(seed=7, start=MON_08, history_days=60)
    return {(sym, tf.value): sim.closed_bars(sym, tf, 1500).reset_index(drop=True) for sym, tf in SERIES}


def rows():
    out = []
    for (sym, tf), bars in series().items():
        for key, sets in PARAM_SETS.items():
            s = STRATEGIES[key]
            for params in sets:
                f = s.signals(bars, params)
                for i in range(len(bars)):
                    r = f.iloc[i]
                    out.append({"sym": sym, "tf": tf, "strategy": key, "params": params, "i": i,
                                "flags": "".join("1" if bool(r[c]) else "0" for c in ("long", "short", "exit_long", "exit_short")),
                                "sl": round(float(r["sl_dist"]), 8) if r["sl_dist"] == r["sl_dist"] else None})
    return out


if __name__ == "__main__":
    data = rows()
    path = Path(__file__).parent / "fixtures" / "golden_gold_signals.json"
    path.write_text(json.dumps(data))
    print(f"wrote {len(data)} rows; entries:", sum(r["flags"][0] == "1" or r["flags"][1] == "1" for r in data))
