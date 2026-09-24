"""The vectorised Strategies must reproduce the pre-vectorisation per-bar behaviour exactly
(golden fixture), and live evaluation of a warmed-up window must agree with a full-series
Backtest (ADR 0005)."""

import json
from pathlib import Path

import pytest

from fxcommand.broker.sim import SimBroker
from fxcommand.broker.types import Timeframe
from fxcommand.strategies import STRATEGIES
from fxcommand.strategies.base import WARMUP_BARS

MON_08 = 1_704_700_800
# every 4th row keeps the suite fast; the full fixture was verified once when vectorising
GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "golden_signals.json").read_text())[::4]


@pytest.fixture(scope="module")
def series():
    sim = SimBroker(seed=5, start=MON_08, history_days=10)
    sim.step(600)
    return {
        ("EURUSD", "M1"): sim.closed_bars("EURUSD", Timeframe.M1, 1200).reset_index(drop=True),
        ("GOLD", "M5"): sim.closed_bars("GOLD", Timeframe.M5, 1200).reset_index(drop=True),
    }


def test_golden_fixture_reproduced(series):
    mismatches = []
    for g in GOLDEN:
        s = STRATEGIES[g["strategy"]]
        p = s.resolve(g["params"])
        need = s.lookback(p) + 5
        bars = series[(g["sym"], g["tf"])]
        window = bars.iloc[g["i"] - need + 1 : g["i"] + 1]
        sig = s.run(window, g["params"], g["pos"])
        same = sig.action == g["action"] and abs(sig.sl_dist - g["sl"]) < 1e-7 and abs(sig.tp_dist - g["tp"]) < 1e-7
        if not same:
            mismatches.append((g, sig.action, sig.sl_dist))
    assert not mismatches, f"{len(mismatches)} of {len(GOLDEN)} differ, e.g. {mismatches[:3]}"


@pytest.mark.parametrize("key", list(STRATEGIES))
def test_warmed_up_live_window_matches_full_series(series, key):
    s = STRATEGIES[key]
    bars = series[("EURUSD", "M1")]
    p = s.resolve({})
    full = s.signals(bars, p)
    need = s.lookback(p) + WARMUP_BARS
    checked = disagree = 0
    for i in range(need, len(bars), 7):
        live = s.signals(bars.iloc[i - need + 1 : i + 1], p).iloc[-1]
        ref = full.iloc[i]
        checked += 1
        if any(bool(live[c]) != bool(ref[c]) for c in ("long", "short", "exit_long", "exit_short")):
            disagree += 1
        assert live["sl_dist"] == pytest.approx(ref["sl_dist"], rel=1e-6)
    assert checked > 50 and disagree == 0


def test_signals_frame_shape(series):
    bars = series[("GOLD", "M5")]
    for s in STRATEGIES.values():
        f = s.signals(bars, {})
        assert len(f) == len(bars)
        assert {"long", "short", "exit_long", "exit_short", "sl_dist", "tp_dist"} <= set(f.columns)
        assert not f.iloc[: s.lookback(s.resolve({})) - 1][["long", "short"]].any().any()  # nothing during warm-up
