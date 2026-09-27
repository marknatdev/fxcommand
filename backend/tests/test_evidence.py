"""Evidence Run and scorecard (spec D6, D9, D11, D23, D24): the key, Weekend Close in the Backtest,
paged read-only history, low priority behind Optimizer Runs, waiting for orders in flight, incomplete
runs, and the Paper record against the backtest's band."""

import asyncio
import pickle
import time

import numpy as np
import pandas as pd
import pytest

from fxcommand.broker import Timeframe
from fxcommand.broker.sim import DAY, SimBroker
from fxcommand.engine import AssignmentIn, DomainError, SessionIn
from fxcommand.learning import evidence as ev
from fxcommand.learning.candidate import Candidate
from fxcommand.learning.costs import CostModel
from fxcommand.learning.paper import EntryGate, ExitRules, PaperTrader
from fxcommand.risk.window import WeekendClose, in_weekend_close
from fxcommand.store import TradeRow

from .conftest import MON_08
from .test_learning_service import LH

H4 = 4 * 3600
TREND = ("GOLD", "H4", "trend_breakout")


# ------------------------------------------------------------------ the key
def test_the_key_changes_with_every_setting_it_depends_on():
    base = ev.EvidenceSpec.of(*TREND)
    same = ev.EvidenceSpec.of(*TREND, params=Candidate.of("trend_breakout", None).param_dict, window={}, weekend_close="")
    assert same.key == base.key  # explicit defaults are the defaults
    variants = [
        ev.EvidenceSpec.of("GOLD", "H1", "trend_breakout"),
        ev.EvidenceSpec.of("EURUSD", "H4", "trend_breakout"),
        ev.EvidenceSpec.of(*TREND, params={"entry": 40}),
        ev.EvidenceSpec.of(*TREND, rules=ExitRules(breakeven=True)),
        ev.EvidenceSpec.of(*TREND, rules=ExitRules(reverse_on_opposite=False)),
        ev.EvidenceSpec.of(*TREND, window={"close_time": "21:00"}),
        ev.EvidenceSpec.of(*TREND, weekend_close="22:30"),
        ev.EvidenceSpec.of(*TREND, weekend_close="21:00"),
        ev.EvidenceSpec(base.symbol, base.timeframe, base.candidate, base.rules, base.window, "", base.cost_version + 1),
    ]
    keys = {v.key for v in variants}
    assert len(keys) == len(variants) and base.key not in keys
    assert base.with_weekend_close("22:30").key == ev.EvidenceSpec.of(*TREND, weekend_close="22:30").key


# --------------------------------------------------------- Weekend Close in the Backtest
FLAT = {"long": False, "short": False, "exit_long": False, "exit_short": False, "sl_dist": 50.0, "tp_dist": 0.0}
LONG = {**FLAT, "long": True}


def run_bars(trader, times, signal_at, price=2000.0):
    closed = []
    for t in times:
        _, c = trader.on_bar(int(t), price, price + 1, price - 1, price, 5.0, LONG if t == signal_at else FLAT)
        closed.extend(c)
    return closed


def test_weekend_close_settles_at_the_close_of_the_bar_that_reaches_friday_close():
    week = [MON_08 - 8 * 3600 + k * H4 for k in range(6 * 5 + 6)]  # Monday 00:00 H4 bars into the next week
    week = [t for t in week if (t // DAY + 3) % 7 < 5]  # trading days only
    fri_20 = next(t for t in week if (t // DAY + 3) % 7 == 4 and (t % DAY) == 20 * 3600)
    costs = CostModel(spread=0.5)
    held = run_bars(PaperTrader(costs), week, signal_at=week[0])
    assert held == []  # without Weekend Close the trend position rides through the weekend
    flat = run_bars(PaperTrader(costs, weekend=WeekendClose("22:30", H4)), week, signal_at=week[0])
    assert [(t.reason, t.close_time) for t in flat] == [("weekend", fri_20)]  # the bar holding 22:30, not Monday's open
    assert flat[0].exit == pytest.approx(2000.0)  # its close (a long exits at the bid)


def test_weekend_close_refuses_an_entry_that_would_fill_inside_the_span():
    fri = MON_08 + 4 * DAY - 8 * 3600  # Friday 00:00
    m5 = [fri + 22 * 3600 + k * 300 for k in range(-3, 4)]  # 21:45 … 22:15 … 22:30 … 22:45
    signal = fri + 22 * 3600 + 25 * 60  # the 22:25 bar: its entry would fill at 22:30
    m5 = sorted(set(m5 + [signal, signal + 300, signal + 600]))
    wk = WeekendClose("22:30", 300)
    assert run_bars(PaperTrader(CostModel(spread=0.5), weekend=wk), m5, signal_at=signal) == []
    tr = PaperTrader(CostModel(spread=0.5), weekend=wk)
    run_bars(tr, m5, signal_at=signal)
    assert tr.position is None
    tr = PaperTrader(CostModel(spread=0.5))
    run_bars(tr, m5, signal_at=signal)
    assert tr.position is not None  # without it the entry fills at 22:30
    assert in_weekend_close(signal + 300, "22:30") and not in_weekend_close(signal, "22:30")


# ------------------------------------------------------------------ pure arithmetic
def test_periods_are_fixed_spans_for_long_histories_and_thirds_otherwise():
    y = lambda year: int(pd.Timestamp(f"{year}-06-01").timestamp())  # noqa: E731
    close = np.array([y(2012), y(2015), y(2019), y(2024), y(2025)])
    rs = np.array([1.0, -1.0, 0.5, 2.0, 0.0])
    fixed = ev.periods(close, rs, int(pd.Timestamp("2010-01-04").timestamp()), y(2026))
    assert [(p["label"], p["n"], p["mean"]) for p in fixed] == [("2010–17", 2, 0.0), ("2018–21", 1, 0.5), ("2022–26", 2, 1.0)]
    first, last = y(2022), y(2025)
    mid = np.array([first + (last - first) * k // 6 for k in (1, 3, 5)])  # one trade in the middle of each third
    thirds = ev.periods(mid, np.array([1.0, 2.0, 3.0]), first, last)
    assert [(p["n"], p["mean"]) for p in thirds] == [(1, 1.0), (1, 2.0), (1, 3.0)]
    assert thirds[0]["from_ts"] == first and thirds[-1]["to_ts"] == last + 1


def test_band_verdict_and_min_lot_risk():
    rs = np.random.default_rng(1).normal(0.1, 1.0, 400)
    lo, hi = ev.band(rs, 40)
    assert lo < rs.mean() < hi and ev.band(rs, 40) == (lo, hi)  # deterministic
    assert ev.band(rs, 400)[1] - ev.band(rs, 400)[0] < hi - lo  # more trades, narrower band
    assert ev.band([], 10) is None and ev.band(rs, 0) is None
    assert ev.verdict(40, lo - 0.01, (lo, hi)) == "below the backtest"
    assert ev.verdict(40, (lo + hi) / 2, (lo, hi)) == "within the backtest"
    assert ev.verdict(5, hi + 1, (lo, hi)) == "too few trades"
    assert ev.verdict(40, 0.0, None) == "no evidence"
    # GOLD: 0.01 lot, a $30 stop, $100 per 1.0 per lot, a $50 account → 60 % of it on one trade
    assert ev.min_lot_risk_pct(0.01, 30.0, 100.0, 50.0) == 60.0
    assert ev.min_lot_risk_pct(0.01, 30.0, 100.0, 0.0) is None


# ------------------------------------------------------------------ paged history
@pytest.mark.parametrize("tf", [Timeframe.M1, Timeframe.H1, Timeframe.H4, Timeframe.D1])
def test_pages_stitch_into_the_same_history_as_one_read(tf):
    sim = SimBroker(seed=3, start=MON_08, history_days=8, deep_history_days=90)
    whole = sim.closed_bars("GOLD", tf, 10**7)
    pages, got = [], 0
    while True:
        page = sim.closed_bars("GOLD", tf, 997, got)
        if len(page) == 0:
            break
        pages.append(page)
        got += len(page)
    stitched = pd.concat(pages[::-1], ignore_index=True)
    pd.testing.assert_frame_equal(stitched.reset_index(drop=True), whole.reset_index(drop=True), check_dtype=False)
    assert sim.closed_bars("GOLD", tf, 5, len(whole) + 3).empty


def test_the_job_pickles_for_the_learning_process():
    sim = SimBroker(seed=3, start=MON_08, history_days=8, deep_history_days=200)
    bars = sim.closed_bars("GOLD", Timeframe.H4, 10**6)
    spec = ev.EvidenceSpec.of(*TREND, weekend_close="22:30")
    args = (bars, spec.candidate, CostModel.from_symbol(sim.symbol_info("GOLD"), 0.6, ref_price=2000.0), spec.rules,
            EntryGate(spec.window, H4), WeekendClose("22:30", H4))
    again = pickle.loads(pickle.dumps(args))
    assert ev.evidence_job(*again) == ev.evidence_job(*args)


# ------------------------------------------------------------------ the service
@pytest.fixture
async def eh(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "CHUNK_BARS", 500)
    h = LH(tmp_path, sim=SimBroker(seed=5, start=MON_08, history_days=10, deep_history_days=600))
    await h.mgr.tick_once()
    reads = []
    real = h.sim.closed_bars

    def recording(symbol, timeframe, count, offset=0):
        reads.append((symbol, Timeframe(timeframe), count, offset))
        return real(symbol, timeframe, count, offset)

    h.sim.closed_bars = recording  # the router and the read-only view reach the sim through getattr
    h.reads = reads
    yield h
    await h.close()


async def run(h, spec, trigger="manual"):
    row = h.L.request_evidence(spec, trigger)
    await h.L.drain()
    return h.L.repo.evidence_row(row["id"])


async def test_an_evidence_run_reads_all_history_in_pages_and_is_found_by_its_key(eh):
    spec = ev.EvidenceSpec.of(*TREND)
    row = await run(eh, spec)
    reads = [r for r in eh.reads if r[1] == Timeframe.H4]
    whole = len(eh.sim.closed_bars("GOLD", Timeframe.H4, 10**7))
    assert row.status == "done" and row.bars == whole and whole > 2000
    assert len(reads) >= whole // 500 and all(count <= ev.CHUNK_BARS for _, _, count, _ in reads)  # never one big read
    assert row.trades == len(row.rs) > 0 and len(row.periods) == 3 and row.first_ts < row.last_ts
    typical = eh.L.spreads.typical("GOLD")
    assert row.costs["spread"] == pytest.approx(2 * typical)  # priced at twice the typical spread
    assert eh.L.evidence_for(spec)["status"] == "match"
    assert eh.L.evidence_for(ev.EvidenceSpec.of(*TREND, rules=ExitRules(trailing=True)))["status"] == "mismatch"
    assert eh.L.evidence_for(ev.EvidenceSpec.of("GOLD", "H1", "session_drift"))["status"] == "none"
    assert [e["key"] for e in eh.L.evidence("GOLD")] == [spec.key] and "rs" not in eh.L.evidence("GOLD")[0]
    assert any("Evidence GOLD H4" in j.message for j in eh.store.journal(limit=50))


async def test_weekend_close_changes_the_gold_trend_evidence_and_the_assignment_shows_both(eh):
    s = await eh.mgr.create_session(SessionIn(name="Trend", daily_loss_pct=90, weekend_close=True,
                                              assignments=[AssignmentIn(symbol="GOLD", timeframe="H4", strategy="trend_breakout")]))
    [a] = eh.store.assignments(s.id)
    spec = eh.L.spec_for(s, a)
    assert spec.weekend_close == "22:30"
    with_wc = await run(eh, spec)
    without = await run(eh, spec.with_weekend_close(None))
    assert with_wc.rs != without.rs and with_wc.key != without.key
    badge = eh.L.assignment_evidence(s, a)
    assert badge["status"] == "match" and badge["evidence"]["id"] == with_wc.id
    assert badge["without_weekend_close"]["evidence"]["id"] == without.id


async def test_refused_while_an_order_is_in_flight(eh):
    eh.mgr._orders_in_flight = 1
    with pytest.raises(DomainError) as e:
        eh.L.request_evidence(ev.EvidenceSpec.of(*TREND))
    assert e.value.code == "busy" and e.value.status == 409
    eh.mgr._orders_in_flight = 0
    assert eh.L.request_evidence(ev.EvidenceSpec.of(*TREND))["status"] == "queued"
    await eh.L.drain()


async def test_pages_wait_while_an_order_is_in_flight(eh):
    row = eh.L.request_evidence(ev.EvidenceSpec.of(*TREND))
    eh.mgr._orders_in_flight = 1  # an entry starts right after the run was accepted
    drain = asyncio.create_task(eh.L.drain())
    await asyncio.sleep(0.6)
    assert eh.L.repo.evidence_row(row["id"]).status == "running"
    assert not [r for r in eh.reads if r[1] == Timeframe.H4 and r[2] <= 500]  # not one page read meanwhile
    eh.mgr._orders_in_flight = 0
    await drain
    assert eh.L.repo.evidence_row(row["id"]).status == "done"


async def test_the_engine_counts_its_order_paths(eh):
    seen = []
    real = eh.sim.market_order

    def spy(*a, **k):
        seen.append(eh.L.order_busy())
        return real(*a, **k)

    eh.sim.market_order = spy
    s = await eh.session("Busy")
    await eh.mgr.start(s.id)
    for _ in range(40):
        if seen:
            break
        await eh.bars(1)
    assert seen and all(seen) and not eh.L.order_busy()


async def test_a_page_that_times_out_leaves_an_incomplete_run_with_the_bars_received(eh, monkeypatch):
    monkeypatch.setattr(ev, "CHUNK_TIMEOUT", 0.3)
    inner = eh.sim.closed_bars

    def slow_second_page(symbol, timeframe, count, offset=0):
        if Timeframe(timeframe) == Timeframe.H4 and offset > 0:
            time.sleep(0.8)
        return inner(symbol, timeframe, count, offset)

    eh.sim.closed_bars = slow_second_page
    row = await run(eh, ev.EvidenceSpec.of(*TREND))
    assert row.status == "incomplete" and row.bars == 500
    assert "stopped after 500 bars" in row.note
    assert eh.L.evidence_for(ev.EvidenceSpec.of(*TREND))["evidence"]["status"] == "incomplete"


async def test_evidence_waits_behind_optimizer_runs_and_never_sends_an_order(eh):
    def boom(*a, **k):
        raise AssertionError("an Evidence Run must never touch an order method")

    eh.sim.market_order = eh.sim.modify = eh.sim.close = boom
    row = eh.L.request_evidence(ev.EvidenceSpec.of(*TREND))
    run_id = eh.L.request_optimize("EURUSD", "M1", "test")  # queued after, runs first
    await eh.L.drain()
    opt = next(r for r in eh.L.repo.runs(limit=5) if r.id == run_id)
    evid = eh.L.repo.evidence_row(row["id"])
    assert evid.status == "done" and opt.finished_wall <= evid.finished_wall
    # the same settings while queued are merged into one run
    a = eh.L.request_evidence(ev.EvidenceSpec.of(*TREND, weekend_close="22:30"))
    b = eh.L.request_evidence(ev.EvidenceSpec.of(*TREND, weekend_close="22:30"))
    assert a["id"] == b["id"]
    await eh.L.drain()


async def test_a_run_with_no_trustworthy_spread_is_refused(tmp_path):
    h = LH(tmp_path, sim=SimBroker(seed=4, start=MON_08 + 16 * 3600 + 20 * 60, history_days=10))  # Tuesday 00:20: GOLD shut
    await h.mgr.tick_once()
    row = await run(h, ev.EvidenceSpec.of("GOLD", "H1", "session_drift"))
    assert row.status == "failed" and "market is shut" in row.note
    await h.close()


# ------------------------------------------------------------------ the scorecard
async def test_the_scorecard_sets_the_paper_record_against_the_backtest_band(eh):
    s = await eh.mgr.create_session(SessionIn(name="Paper Trend", daily_loss_pct=90, execution="paper",
                                              assignments=[AssignmentIn(symbol="GOLD", timeframe="H4", strategy="trend_breakout")]))
    await eh.mgr.create_session(SessionIn(name="Other", daily_loss_pct=90, assignments=[AssignmentIn(symbol="EURUSD", timeframe="M15", strategy="ema_cross")]))
    [a] = eh.store.assignments(s.id)
    row = await run(eh, eh.L.spec_for(s, a))
    mine = Candidate.of("trend_breakout", None).key
    for i, r in enumerate([-1.0] * 12):  # a Paper record far below what the backtest allows
        eh.store.add_trade(TradeRow(ticket=-(i + 1), session_id=s.id, magic=a.magic, symbol="GOLD", side="long", volume=0.01, strategy="trend_breakout",
                                    timeframe="H4", candidate_key=mine, open_time=MON_08, open_price=2000, status="closed",
                                    close_time=MON_08 + 3600, close_price=1990, profit=r * 5.0, risk_amount=5.0, paper=True))
    for ticket, key in ((-50, Candidate.of("trend_breakout", {"entry": 40}).key), (-51, ""), (-52, "")):  # another parameter set; unrecorded
        eh.store.add_trade(TradeRow(ticket=ticket, session_id=s.id, magic=a.magic, symbol="GOLD", side="long", volume=0.01, strategy="trend_breakout",
                                    timeframe="H4", candidate_key=key, open_time=MON_08, open_price=2000, status="closed",
                                    close_time=MON_08 + 3600, close_price=2100, profit=50.0, risk_amount=5.0, paper=True))
    eh.store.add_trade(TradeRow(ticket=-99, session_id=s.id, magic=a.magic, symbol="GOLD", side="long", volume=0.01, strategy="trend_breakout",
                                timeframe="H4", open_time=MON_08, open_price=2000, status="closed", close_time=MON_08, close_price=2100,
                                profit=500.0, risk_amount=5.0, paper=True, paper_epoch=7))  # an archived epoch: not counted
    cards = {(c["symbol"], c["timeframe"]): c for c in await eh.L.scorecard()}
    gold = cards[("GOLD", "H4")]
    assert gold["session"]["execution"] == "paper" and gold["evidence"]["evidence"]["id"] == row.id
    assert gold["paper"]["trades"] == 12 and gold["paper"]["mean_r"] == -1.0  # only this parameter set's trades
    assert gold["paper"]["unattributed"] == 2 and "2 earlier trades" in gold["note"]
    assert gold["band"] == list(ev.band(row.rs, 12)) and gold["verdict"] == "below the backtest"
    info, acct = eh.sim.symbol_info("GOLD"), eh.sim.account()
    expected = ev.min_lot_risk_pct(info.volume_min, gold["stop"], info.trade_tick_value / info.trade_tick_size, acct.balance)
    assert gold["min_lot_risk_pct"] == expected and expected > 0
    other = cards[("EURUSD", "M15")]
    assert other["verdict"] == "no evidence" and other["band"] is None and other["paper"]["trades"] == 0


def test_optimizer_runs_apply_the_sessions_weekend_close():
    from fxcommand.learning.optimizer import optimize_job
    from fxcommand.learning.service import LearningService
    from fxcommand.risk import TradingWindow

    sim = SimBroker(seed=5, start=MON_08, history_days=10, deep_history_days=600)
    bars = sim.closed_bars("GOLD", Timeframe.H4, 10**6)
    cm = CostModel.from_symbol(sim.symbol_info("GOLD"), 0.6, ref_price=float(bars["close"].iloc[-1]))
    args = LearningService._job_args(bars, Candidate.of("trend_breakout", None), cm, ExitRules(), 2, 1, TradingWindow(), Timeframe.H4, set())
    off = optimize_job(*pickle.loads(pickle.dumps(args)))
    on = optimize_job(*pickle.loads(pickle.dumps(args[:-1] + ("22:30",))))
    assert off.champion.wf.to_dict() != on.champion.wf.to_dict()


# ------------------------------------------------------------ swap beside the Cost Check
async def test_the_cost_check_shows_swap_from_the_evidence_and_never_blocks_on_it(eh):
    s = await eh.mgr.create_session(SessionIn(name="Swap", daily_loss_pct=90, assignments=[
        AssignmentIn(symbol="GOLD", timeframe="H4", strategy="trend_breakout"), AssignmentIn(symbol="GOLD", timeframe="H1", strategy="session_drift")]))
    before = {c["timeframe"]: c for c in await eh.mgr.cost_check(s)}
    assert before["H4"]["swap_nights"] is None and before["H4"]["swap_r"] == 0.0  # nothing known yet
    trend = await run(eh, ev.EvidenceSpec.of(*TREND))
    drift = await run(eh, ev.EvidenceSpec.of("GOLD", "H1", "session_drift"))
    assert trend.avg_nights > 1 and trend.long_share == 1.0  # long-only, held for days
    assert drift.avg_nights == 0  # the Reopen trade is flat by 04:00: never pays swap
    after = {c["timeframe"]: c for c in await eh.mgr.cost_check(s)}
    h4, h1 = after["H4"], after["H1"]
    info, tick = eh.sim.symbol_info("GOLD"), eh.sim.tick("GOLD")
    per_night = CostModel.from_symbol(info, 0.0).swap_per_night("long", tick.bid)
    assert per_night < 0 and h4["swap_r"] == pytest.approx(per_night * trend.avg_nights / h4["stop"], abs=1e-3)
    assert h4["swap_source"] == "Evidence" and h4["swap_nights"] == trend.avg_nights
    assert h4["cost_r"] == before["H4"]["cost_r"] and h4["allowed"] == before["H4"]["allowed"]  # swap never changes the verdict
    assert h1["swap_r"] == 0.0 and h1["swap_nights"] == 0


async def test_nights_come_from_shadow_trades_when_there_is_no_evidence(eh):
    from fxcommand.store.models import ShadowTradeRow

    cand = Candidate.of("trend_breakout", None)
    wed = MON_08 + 2 * DAY
    with eh.L.repo._db() as db:
        db.add(ShadowTradeRow(symbol="GOLD", timeframe="H4", candidate_key=cand.key, side="long", open_ts=wed, close_ts=wed + DAY,
                              signal_ts=wed, entry=2000, sl=1990, tp=0, risk=10, status="closed", r=1.0))
        db.commit()
    hold = eh.L.expected_hold("GOLD", "H4", "trend_breakout", None)
    assert hold == {"nights": 3.0, "long_share": 1.0, "trades": 1, "source": "Shadow Trades"}  # Wednesday night counts three
    assert eh.L.expected_hold("GOLD", "H1", "session_drift", None) is None
