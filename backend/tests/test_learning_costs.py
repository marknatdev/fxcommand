"""Milestone 4: Shadow Trades and Backtests priced with the Cost Model (price-scaled spread and
slippage, swap per rollover), the Trading Window judged at the fill, a typical spread that never
comes from the daily break, picklable Optimizer Run arguments, and Shadow Trade retention."""

import pickle

import pandas as pd
import pytest

from fxcommand.broker.sim import SimBroker
from fxcommand.broker.types import Timeframe
from fxcommand.engine import AssignmentIn, SessionIn
from fxcommand.learning.candidate import Candidate
from fxcommand.learning.costs import SWAP_POINTS, CostModel
from fxcommand.learning.optimizer import optimize_job
from fxcommand.learning.paper import Costs, EntryGate, ExitRules, PaperTrader, backtest
from fxcommand.learning.repo import KEEP_SHADOWS
from fxcommand.learning.service import LearningService
from fxcommand.risk import TradingWindow

from .conftest import MON_08
from .test_learning_service import LH

DAY = 86400
MON = MON_08 - 8 * 3600  # Monday 00:00 server time
NO_SIGNAL = {"long": False, "short": False, "exit_long": False, "exit_short": False, "sl_dist": float("nan"), "tp_dist": 0.0}


def long_signal(sl=1.0):
    return {**NO_SIGNAL, "long": True, "sl_dist": sl}


# ------------------------------------------------------------------ EntryGate
@pytest.mark.parametrize(
    "fill_at, bar_s, fill_window, expected",
    [
        (MON + DAY, 4 * 3600, None, True),  # H4 00:00 bar: the 23:55-00:10 blackout ends inside the bar
        (MON, DAY, None, True),  # D1 Monday 00:00: the week opens at 00:10
        (MON + 4 * DAY + 23 * 3600, 3600, None, False),  # Friday 23:00 H1 bar: shut until Monday
        (MON + DAY, 300, None, False),  # M5 00:00 bar: still in the blackout at its close
        (MON + DAY + 3600, 3600, 300, True),  # session_drift at the 01:00 reopen: open at once
    ],
)
def test_the_window_is_judged_at_the_next_tradable_time_within_the_fill_limit(fill_at, bar_s, fill_window, expected):
    assert EntryGate(TradingWindow().to_dict(), bar_s, fill_window)(fill_at) is expected


def test_a_short_fill_window_drops_what_the_bar_would_allow():
    late = {**TradingWindow().to_dict(), "blackouts": [{"start": "00:55", "end": "01:10"}]}
    t = MON + DAY + 3600  # Tuesday 01:00; the window opens at 01:10
    assert EntryGate(late, 3600).for_strategy("trend_breakout")(t) is True
    assert EntryGate(late, 3600).for_strategy("session_drift")(t) is False  # 300 s fill window
    assert EntryGate(None, 3600)(t) is True


def test_entries_are_checked_when_they_fill_not_when_they_are_signalled():
    shut = {**TradingWindow().to_dict(), "close_time": "12:00"}  # closes Friday 12:00
    tr = PaperTrader(Costs(0.0), allow_entry=EntryGate(shut, 3600))
    fri = MON + 4 * DAY
    tr.on_bar(fri + 10 * 3600, 100, 100, 100, 100, 1.0, long_signal())  # signal on the 10:00 bar
    opened, _ = tr.on_bar(fri + 11 * 3600, 100, 100, 100, 100, 1.0, NO_SIGNAL)  # fills 11:00: open
    assert opened is not None
    tr2 = PaperTrader(Costs(0.0), allow_entry=EntryGate(shut, 3600))
    tr2.on_bar(fri + 11 * 3600, 100, 100, 100, 100, 1.0, long_signal())
    opened, _ = tr2.on_bar(fri + 12 * 3600, 100, 100, 100, 100, 1.0, NO_SIGNAL)  # would fill 12:00: shut
    assert opened is None and tr2.position is None


# ------------------------------------------------------------------ Cost Model
def test_spread_and_slippage_scale_with_the_bars_price():
    cm = CostModel(spread=0.6, slippage=0.1, ref_price=2000.0)
    tr = PaperTrader(cm)
    tr.on_bar(MON, 1000, 1000, 1000, 1000, 1.0, long_signal(sl=10.0))
    opened, _ = tr.on_bar(MON + 60, 1000, 1000, 1000, 1000, 1.0, NO_SIGNAL)
    assert opened.entry == pytest.approx(1000 + 0.3 + 0.05)  # half the price, half the costs


def test_swap_is_charged_per_rollover_with_three_nights_on_wednesday():
    cm = CostModel(spread=0.0, swap_long=-80.0, swap_short=20.0, swap_mode=SWAP_POINTS, point=0.01, triple_weekday=2)
    tr = PaperTrader(cm)
    tue = MON + DAY + 12 * 3600
    tr.on_bar(tue, 100, 100, 100, 100, 1.0, long_signal(sl=4.0))
    tr.on_bar(tue + 3600, 100, 100, 100, 100, 1.0, NO_SIGNAL)  # filled Tuesday 13:00
    [t] = tr.finish(tue + 2 * DAY, 100)  # Thursday 12:00: the Tuesday night and Wednesday's triple
    assert t.r == pytest.approx((1 + 3) * -0.80 / 4.0)
    assert cm.swap_for("short", 100, tue, tue + 2 * DAY) == pytest.approx(4 * 0.20)


def test_backtest_takes_a_cost_model_and_an_entry_gate():
    sim = SimBroker(seed=5, start=MON_08, history_days=20)
    bars = sim.closed_bars("GOLD", Timeframe.H1, 400)
    info = sim.symbol_info("GOLD")
    cm = CostModel.from_symbol(info, spread=0.30, ref_price=float(bars["close"].iloc[-1]))
    gate = EntryGate(TradingWindow().to_dict(), 3600)
    trades = backtest(bars, Candidate.of("session_drift", {}), cm, ExitRules(reverse_on_opposite=False), gate)
    assert trades and all((pd.Timestamp(t.open_time, unit="s").hour == 1) for t in trades)
    fixed = backtest(bars, Candidate.of("session_drift", {}), Costs(0.30, info.point), ExitRules(reverse_on_opposite=False), gate)
    assert [t.open_time for t in trades] == [t.open_time for t in fixed]  # same trades, only priced differently


def test_optimizer_run_arguments_pickle_for_the_worker_process():
    sim = SimBroker(seed=5, start=MON_08, history_days=10)
    bars = sim.closed_bars("EURUSD", Timeframe.M5, 1500)
    cm = CostModel.from_symbol(sim.symbol_info("EURUSD"), spread=0.00012, ref_price=float(bars["close"].iloc[-1]))
    args = LearningService._job_args(bars, Candidate.of("ema_cross", {}), cm, ExitRules(), 8, 1, TradingWindow(), Timeframe.M5, set())
    back = pickle.loads(pickle.dumps(args))
    gate = pickle.loads(pickle.dumps(EntryGate(TradingWindow().to_dict(), 300, 300)))
    assert gate(MON + DAY) is False and gate.limit == 300
    result = optimize_job(*back)
    assert result.bars == len(bars)


# ---------------------------------------------------------- typical spread
async def test_gold_reopen_shadow_trades_are_priced_at_the_typical_spread_not_the_break(tmp_path):
    sim = SimBroker(seed=4, start=MON_08 + 10 * 3600, history_days=10)  # Monday 18:00
    h = LH(tmp_path, sim=sim)
    await h.mgr.tick_once()
    s = await h.mgr.create_session(
        SessionIn(name="Reopen", daily_loss_pct=90, assignments=[AssignmentIn(symbol="GOLD", timeframe="H1", strategy="session_drift")])
    )
    await h.mgr.start(s.id)
    await h.bars(int(1.6 * DAY / 60))  # through two reopens
    rows = h.L.repo.shadow_closed("GOLD", "H1")
    assert len(rows) >= 2
    bars = sim.closed_bars("GOLD", Timeframe.M1, 3 * 1440).set_index("time")
    for r in rows:
        assert pd.Timestamp(r.open_ts, unit="s").hour == 1
        paid = r.entry - bars.loc[r.open_ts, "open"]
        assert 0.25 < paid < 0.45, paid  # ~30 points + slippage, not 120 (rollover) or 70 (reopen)
    await h.close()


# ------------------------------------------------------------------ retention
def test_retention_keeps_the_newest_200_closed_shadow_trades_per_candidate_whatever_their_age(tmp_path):
    from fxcommand.learning.repo import LearningRepo
    from fxcommand.store import Store

    repo = LearningRepo(Store(f"sqlite:///{tmp_path / 'r.db'}"))
    old = MON - 400 * DAY

    def add(key, n, status="closed"):
        for i in range(n):
            row = repo.add_shadow(symbol="GOLD", timeframe="H4", candidate_key=key, side="long", signal_ts=old + i, open_ts=old + i * 3600,
                                  entry=1.0, sl=0.9, tp=0.0, risk=0.1)
            if status == "closed":
                repo.close_shadow(row.id, close_ts=old + i * 3600 + 60, exit=1.0, r=0.0, reason="tp")

    add("a", 250)
    add("b", 10)
    add("a", 1, status="open")
    repo.cleanup(MON - 180 * DAY)
    kept = repo.shadow_closed("GOLD", "H4", "a", limit=10_000)
    assert len(kept) == KEEP_SHADOWS == 200
    assert min(r.close_ts for r in kept) == old + 50 * 3600 + 60  # the newest ones
    assert len(repo.shadow_closed("GOLD", "H4", "b", limit=10_000)) == 10
    assert len(repo.open_shadows("GOLD", "H4")) == 1
