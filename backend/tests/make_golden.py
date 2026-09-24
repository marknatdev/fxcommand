"""Capture Strategy.run output (pre-vectorisation) as a golden fixture. Run once: uv run python tests/make_golden.py"""

import json
from pathlib import Path

from fxcommand.broker.sim import SimBroker
from fxcommand.broker.types import Timeframe
from fxcommand.strategies import STRATEGIES

MON_08 = 1_704_700_800
PARAM_SETS = {
    "ema_cross": [{}, {"fast": 3, "slow": 8, "trend_filter": True, "trend_ema": 50}],
    "donchian_breakout": [{}, {"period": 8, "exit_period": 4}],
    "rsi_reversion": [{}, {"rsi_period": 7, "oversold": 35, "overbought": 65, "adx_max": 40}],
}


def main() -> None:
    sim = SimBroker(seed=5, start=MON_08, history_days=10)
    sim.step(600)
    out = []
    for sym, tf in (("EURUSD", Timeframe.M1), ("GOLD", Timeframe.M5)):
        bars = sim.closed_bars(sym, tf, 1200).reset_index(drop=True)
        for key, sets in PARAM_SETS.items():
            s = STRATEGIES[key]
            for params in sets:
                p = s.resolve(params)
                need = s.lookback(p) + 5  # the window the engine fed run() before vectorisation
                for i in range(need, len(bars), 3):
                    window = bars.iloc[i - need + 1 : i + 1]
                    for pos in (None, "long", "short"):
                        sig = s.run(window, params, pos)
                        out.append(
                            {
                                "sym": sym, "tf": tf.value, "strategy": key, "params": params, "i": i, "pos": pos,
                                "action": sig.action, "sl": round(sig.sl_dist, 8), "tp": round(sig.tp_dist, 8),
                            }
                        )
    path = Path(__file__).parent / "fixtures" / "golden_signals.json"
    path.write_text(json.dumps(out))
    print(f"wrote {len(out)} rows; actions:", {a: sum(r['action'] == a for r in out) for a in ('long', 'short', 'exit', 'none')})


if __name__ == "__main__":
    main()
