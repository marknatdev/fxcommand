"""spec-btc-strategies steps 1–3: swap charged every night for a symbol that trades every day, the
recorded-spread floor, the Symbol Cost Profile and Trusted History (Evidence and research)."""

import calendar
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from fxcommand.learning import evidence as ev
from fxcommand.learning import research
from fxcommand.learning.candidate import Candidate
from fxcommand.learning.costs import (
    COST_MODEL_VERSION, RESEARCH_START, SWAP_POINTS, SYMBOL_PROFILES, CostModel, SymbolCostProfile, profile, rollover_nights,
    swap_every_night, trusted_from,
)
from fxcommand.learning.paper import Costs, ExitRules, PaperTrader, backtest, bar_spreads

DAY = 86400
MON = calendar.timegm((2026, 9, 21, 0, 0, 0))  # a Monday, server time
Y2018 = calendar.timegm((2018, 1, 1, 0, 0, 0))
NO_SIGNAL = {"long": False, "short": False, "exit_long": False, "exit_short": False, "sl_dist": float("nan"), "tp_dist": 0.0}


def btc_info(**kw):
    base = dict(name="BTCUSD", point=0.01, trade_tick_size=0.01, trade_tick_value=0.01, swap_mode=SWAP_POINTS,
                swap_long=-3500.13, swap_short=-2333.42, swap_rollover3days=7)
    return SimpleNamespace(**{**base, **kw})


def gold_info():
    return SimpleNamespace(name="GOLD", point=0.01, trade_tick_size=0.01, trade_tick_value=1.0, swap_mode=SWAP_POINTS,
                           swap_long=-86.84, swap_short=19.79, swap_rollover3days=3)


# ------------------------------------------------------------------ swap every night
def test_a_symbol_without_a_valid_triple_day_pays_every_night_weekends_included():
    assert swap_every_night(7) and not swap_every_night(3) and not swap_every_night(0)
    # Friday 12:00 to Monday 12:00: Saturday, Sunday and Monday midnights
    assert rollover_nights(MON + 4 * DAY + 12 * 3600, MON + 7 * DAY + 12 * 3600, every_night=True) == 3
    # the Monday–Friday rule charges nothing for Saturday and Sunday midnights (Friday ends a day: 1)
    assert rollover_nights(MON + 4 * DAY + 12 * 3600, MON + 7 * DAY + 12 * 3600) == 1
    assert rollover_nights(MON, MON + 7 * DAY, every_night=True) == 7


def test_btc_costs_count_every_night_and_gold_keeps_the_wednesday_triple():
    btc = CostModel.from_symbol(btc_info(), spread=40.0, ref_price=84_000.0)
    assert btc.swap_every_night and btc.swap_nights_key == "n7"
    gold = CostModel.from_symbol(gold_info(), spread=0.57)
    assert not gold.swap_every_night and gold.triple_weekday == 2 and gold.swap_nights_key == "n5t2"
    # nine nights held, at today's price: 9 x -3500.13 points
    swap = btc.swap_for("long", 84_000.0, MON + 12 * 3600, MON + 9 * DAY + 12 * 3600)
    assert swap == pytest.approx(9 * -35.0013)
    # half the price, half the swap (points swap scales with price like every other cost)
    assert btc.swap_for("short", 42_000.0, MON, MON + DAY) == pytest.approx(-23.3342 / 2)


def test_a_symbol_that_reports_no_triple_day_keeps_wednesday():
    for info in (btc_info(name="X", swap_rollover3days=None), SimpleNamespace(name="X", point=0.01)):
        cm = CostModel.from_symbol(info, spread=1.0)
        assert not cm.swap_every_night and cm.triple_weekday == 2


# ------------------------------------------------------------------ the recorded-spread floor
def test_the_profile_floor_applies_to_btc_only_and_scales_with_the_evidence_multiplier():
    btc = CostModel.from_symbol(btc_info(), spread=80.0, ref_price=84_000.0, floor_mult=2.0)
    assert btc.bar_floor == 2.0
    assert btc.spread_at(42_000.0) == pytest.approx(40.0)  # scaled only: no recorded spread
    assert btc.spread_at(42_000.0, recorded=126.0) == pytest.approx(252.0)  # 2 x the recorded $126
    assert btc.spread_at(84_000.0, recorded=30.0) == pytest.approx(80.0)  # the scaled figure is larger
    gold = CostModel.from_symbol(gold_info(), spread=0.57, ref_price=4264.0, floor_mult=2.0)
    assert gold.bar_floor == 0.0 and gold.spread_at(4264.0, recorded=5.0) == pytest.approx(0.57)
    assert Costs(1.0).spread_at(10.0, recorded=5.0) == 1.0


def test_a_long_fills_at_the_recorded_spread_when_it_is_wider():
    costs = CostModel(spread=40.0, bar_floor=1.0, point=0.01, ref_price=84_000.0)
    tr = PaperTrader(costs, ExitRules(reverse_on_opposite=False))
    tr.on_bar(MON, 84_000, 84_100, 83_900, 84_000, 500.0, {**NO_SIGNAL, "long": True, "sl_dist": 1000.0})
    opened, _ = tr.on_bar(MON + 3600, 84_000, 84_100, 83_900, 84_000, 500.0, NO_SIGNAL, bar_spread=100.0)
    assert opened.entry == pytest.approx(84_100.0)


def bars_with_spread(n=600, spread_points=0.0, start=MON):
    rng = np.random.default_rng(3)
    close = 50_000 + np.cumsum(rng.normal(0, 150, n))
    return pd.DataFrame({
        "time": start + np.arange(n) * 4 * 3600, "open": close, "high": close + 200, "low": close - 200, "close": close,
        "volume": 100.0, "spread": spread_points,
    })


def test_backtests_read_the_bars_spread_only_when_the_costs_floor_it():
    costs = CostModel(spread=40.0, bar_floor=1.0, point=0.01, ref_price=50_000.0)
    assert bar_spreads(bars_with_spread(5, 5000.0), costs).tolist() == [50.0] * 5
    assert bar_spreads(bars_with_spread(5, 5000.0), CostModel(spread=40.0)) is None
    assert bar_spreads(bars_with_spread(5).drop(columns="spread"), costs) is None
    cand = Candidate.of("trend_breakout", {"entry": 20, "exit": 10})
    cheap = backtest(bars_with_spread(spread_points=0.0), cand, costs)
    dear = backtest(bars_with_spread(spread_points=30_000.0), cand, costs)  # $300 recorded
    assert cheap and dear
    assert np.mean([t.r for t in dear]) < np.mean([t.r for t in cheap])
    assert dear[0].entry - cheap[0].entry == pytest.approx(300.0 - 40.0 * cheap[0].entry / 50_000.0, rel=0.02)


# ------------------------------------------------------------------ profile and Trusted History
def test_the_profile_map_is_pinned():
    assert SYMBOL_PROFILES == {"BTCUSD": SymbolCostProfile(history_from=Y2018, bar_spread_floor=True)}
    assert profile("GOLD") == SymbolCostProfile() and COST_MODEL_VERSION == 2
    assert trusted_from("BTCUSD") == Y2018 and trusted_from("GOLD") == RESEARCH_START == research.RESEARCH_START


def test_the_evidence_key_carries_the_trusted_history_start():
    btc = ev.EvidenceSpec.of("BTCUSD", "H4", "trend_breakout", {"entry": 100, "exit": 50})
    gold = ev.EvidenceSpec.of("GOLD", "H4", "trend_breakout")
    assert btc.history_from == Y2018 and btc.key.endswith(f"|c2|h{Y2018}")
    assert gold.key.endswith(f"|c2|h{RESEARCH_START}")
    assert btc.row_fields()["history_from"] == Y2018
    assert btc.with_weekend_close("22:30").history_from == Y2018


def test_evidence_counts_only_trades_signalled_inside_the_trusted_history():
    bars = bars_with_spread(n=3000, start=Y2018 - 600 * 4 * 3600)
    cand = Candidate.of("trend_breakout", {"entry": 20, "exit": 10})
    costs = CostModel(spread=40.0)
    whole = ev.evidence_job(bars, cand, costs, ExitRules(), None, None)
    trusted = ev.evidence_job(bars, cand, costs, ExitRules(), None, None, Y2018)
    assert 0 < trusted["trades"] < whole["trades"] and trusted["first_ts"] == Y2018
    assert all(p["from_ts"] >= Y2018 for p in trusted["periods"])


def test_research_judges_from_the_trusted_history_start():
    bars = bars_with_spread(n=3000, start=Y2018 - 600 * 4 * 3600)
    cand = Candidate.of("trend_breakout", {"entry": 20, "exit": 10})
    r = research.evaluate_hypothesis(bars, cand, None, CostModel(spread=40.0), ExitRules(), None, None, 1, history_from=Y2018)
    assert r["first_ts"] == Y2018
