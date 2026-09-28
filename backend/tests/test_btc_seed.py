"""spec-btc-strategies steps 5–7: the BTC research seeded into the trial ledger (the spent holdout is
refused), BTCUSD in the SimBroker, weekend paths at explicit timestamps, and the Cost Check's swap
for a symbol charged every night."""

import calendar

import pytest

from fxcommand.broker.sim import DAY, DEFAULT_SYMBOLS, LEGACY_SYMBOLS
from fxcommand.engine import AssignmentIn, DomainError, SessionIn
from fxcommand.learning.candidate import Candidate
from fxcommand.learning.costs import CostModel
from fxcommand.learning.paper import EntryGate
from fxcommand.learning.seeds import BTC_2026_09
from fxcommand.risk import TradingWindow

from .conftest import MON_08
from .test_evidence import eh  # noqa: F401 (fixture)

BTC_TREND = {"entry": 100, "exit": 50, "atr_period": 20, "sl_atr": 2.0, "tp_atr": 0.0, "allow_short": False}
SAT = MON_08 + 5 * DAY  # Saturday 08:00 server time


def test_the_seed_is_pinned():
    s = BTC_2026_09
    assert s["id"] == "btc-2026-09" and s["symbol"] == "BTCUSD" and len(s["trials"]) == 24
    assert sum(t["timeframe"] == "H4" for t in s["trials"]) == 8 and sum(t["timeframe"] == "H1" for t in s["trials"]) == 16
    h = s["holdout"]
    assert (h["timeframe"], h["strategy"], h["params"], h["status"]) == ("H4", "trend_breakout", BTC_TREND, "passed")
    assert h["data_from"] == calendar.timegm((2025, 9, 1, 0, 0, 0)) == s["data_to"] + 1
    assert h["result"]["n"] == 11 and h["result"]["closed_only"] == {"n": 10, "R": -0.685}


async def test_the_research_is_seeded_once_and_its_holdout_is_never_scored_again(eh):  # noqa: F811
    assert eh.L.seed_research() == 25
    assert eh.L.seed_research() == 0  # idempotent: a restart adds nothing
    counts = eh.L.repo.trial_counts()
    assert counts[("BTCUSD", "H4")] == {"trials": 9, "holdout_uses": 1} and counts[("BTCUSD", "H1")] == {"trials": 16, "holdout_uses": 0}
    with pytest.raises(DomainError) as e:
        await eh.L.evaluate_holdout("BTCUSD", "H4", "trend_breakout", BTC_TREND, "a second look")
    assert e.value.code == "holdout_used"
    # the same parameters in another order resolve to the same Candidate key
    with pytest.raises(DomainError):
        await eh.L.evaluate_holdout("BTCUSD", "H4", "trend_breakout", {"exit": 50, "entry": 100}, "reworded")


def test_btcusd_joins_the_default_market_only():
    assert "BTCUSD" in {s.name for s in DEFAULT_SYMBOLS} and "BTCUSD" not in {s.name for s in LEGACY_SYMBOLS}


def test_btcusd_in_the_sim_prices_like_xm(eh):  # noqa: F811
    info, tick = eh.sim.symbol_info("BTCUSD"), eh.sim.tick("BTCUSD")
    assert tick.ask - tick.bid == pytest.approx(40.0) and info.contract_size == 1 and info.trade_tick_value == pytest.approx(0.01)
    cm = CostModel.from_symbol(info, 40.0, ref_price=tick.bid)
    assert cm.swap_every_night and cm.bar_floor == 1.0


def test_an_always_open_window_admits_weekend_entries():
    window = TradingWindow.from_dict({"enabled": False})
    assert window.is_open(SAT) and window.is_open(SAT + DAY + 23 * 3600 + 58 * 60)
    assert EntryGate({"enabled": False}, 4 * 3600)(SAT)
    assert not EntryGate(TradingWindow().to_dict(), 4 * 3600)(SAT)  # the default window skips the weekend


async def test_the_cost_check_counts_btc_swap_every_night(eh):  # noqa: F811
    from fxcommand.store.models import ShadowTradeRow

    cand = Candidate.of("trend_breakout", BTC_TREND)
    fri = MON_08 + 4 * DAY
    with eh.L.repo._db() as db:  # Friday 08:00 to Monday 08:00: three midnights
        db.add(ShadowTradeRow(symbol="BTCUSD", timeframe="H4", candidate_key=cand.key, side="long", open_ts=fri, close_ts=fri + 3 * DAY,
                              signal_ts=fri, entry=84_000, sl=82_000, tp=0, risk=2000, status="closed", r=1.0))
        db.commit()
    s = await eh.mgr.create_session(SessionIn(name="BTC Trend", daily_loss_pct=90, window={"enabled": False}, assignments=[
        AssignmentIn(symbol="BTCUSD", timeframe="H4", strategy="trend_breakout", params=BTC_TREND)]))
    [c] = await eh.mgr.cost_check(s)
    assert c["swap_nights"] == 3.0 and c["swap_source"] == "Shadow Trades"  # the Monday–Friday rule would say 1
    assert c["swap_r"] < 0 and c["cost_r"] < 0.15 and c["swap_every_night"]


def test_the_review_skill_names_the_btc_arena_and_forbids_cost_edits():
    from pathlib import Path

    skill = (Path(__file__).resolve().parents[2] / ".claude" / "skills" / "gold-review" / "SKILL.md").read_text(encoding="utf-8")
    assert "**BTCUSD H4** — BTC Trend" in skill and "never try to score it again" in skill
    assert "Never change how costs are priced or judged in a review PR" in skill and "SYMBOL_PROFILES" in skill
