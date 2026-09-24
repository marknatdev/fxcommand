"""LearningService wired into the real engine on SimBroker: Shadow Trades match live trades,
Promotions/Rollbacks only when flat, auto-promotion never on live, the Signal Filter gates entries,
learning failures never stop trading, and pure noise never gets promoted."""

import numpy as np
import pandas as pd
import pytest

from fxcommand.broker.sim import SimBroker
from fxcommand.engine import DomainError
from fxcommand.learning import signal_filter as sf
from fxcommand.learning.candidate import Candidate
from fxcommand.learning.objective import r_stats
from fxcommand.learning.optimizer import optimize
from fxcommand.learning.paper import Costs
from fxcommand.learning.service import LearningService, filter_key

from .conftest import FAST, MON_08, Harness

LOOSE = {"max_positions_global": 20, "daily_loss_pct_global": 90}


class LH(Harness):
    """Harness + LearningService."""

    def __init__(self, tmp_path, sim=None, db_name="l.db"):
        super().__init__(tmp_path, sim=sim, db_name=db_name)
        self.L = LearningService(self.store, self.thread, self.journal, "sim")
        self.mgr.learning = self.L
        self.L.start()
        self.store.update_app_settings({"learning_candidates": 30, "learning_bars": 3000})
        from fxcommand.risk import RiskLimits

        self.store.set_global_limits(RiskLimits(**{**RiskLimits().to_dict(), **LOOSE}))

    async def session(self, name="S1", symbols=("EURUSD",), strategy="ema_cross", params=FAST, **kw):
        kw.setdefault("daily_loss_pct", 90)
        return await super().session(name, symbols, strategy, params, **kw)

    def assignment(self, s, symbol="EURUSD"):
        return next(a for a in self.store.assignments(s.id) if a.symbol == symbol)

    def kinds(self, sid=None):
        return [j.kind for j in self.store.journal(session_id=sid, limit=10_000)]

    async def close(self):
        await self.L.stop()
        super().close()


@pytest.fixture
async def lh(tmp_path):
    h = LH(tmp_path)
    await h.mgr.tick_once()
    yield h
    await h.close()


def add_challenger(h, cand, oos=None, champ_oos=None, robust=True, started=0):
    h.L.repo.ensure_candidate(cand)
    return h.L.repo.add_challenger(
        symbol="EURUSD", timeframe="M1", candidate_key=cand.key, started_ts=started,
        oos=(oos or r_stats([0.6, -1, 1.2, 0.8] * 15)).to_dict(), champion_oos=(champ_oos or r_stats([0.1, -1, 0.9] * 10)).to_dict(),
        trials=10, robust=robust,
    )


def add_shadows(h, key, rs, start_ts):
    for i, r in enumerate(rs):
        h.L.repo.add_shadow(
            symbol="EURUSD", timeframe="M1", candidate_key=key, side="long", signal_ts=start_ts + i * 60, open_ts=start_ts + i * 60 + 60,
            close_ts=start_ts + i * 60 + 120, entry=1.0, exit=1.0, risk=0.001, r=r, reason="tp" if r > 0 else "sl", status="closed",
            features=[0.0] * 12, p_win=0.5,
        )


# ------------------------------------------------------------------ fidelity
async def test_champion_shadow_trades_match_live_trades(lh):
    s = await lh.session()
    await lh.mgr.start(s.id)
    await lh.bars(160)
    champ = lh.L.champion_of(lh.assignment(s))
    live = [t for t in lh.store.trades(session_id=s.id, status="closed")]
    shadow = {t.open_ts: t for t in lh.L.repo.shadow_closed("EURUSD", "M1", champ.key)}
    assert len(live) >= 10
    matched, diffs = 0, []
    for t in live:
        sh = shadow.get(t.open_time)
        if sh is None:
            continue
        matched += 1
        assert sh.side == t.side
        live_reason = {"expert": ("exit", "reverse")}.get(t.close_reason, (t.close_reason,))
        assert sh.reason in live_reason, (t.close_reason, sh.reason)
        diffs.append(abs(sh.r - t.profit / t.risk_amount))
    assert matched / len(live) >= 0.95, f"only {matched}/{len(live)} live trades have an identical Shadow Trade"
    # shadow pays 1 point of slippage per fill; live fills in the simulator have none
    assert float(np.median(diffs)) < 0.08 and max(diffs) < 0.35, (np.median(diffs), max(diffs))


async def test_learning_never_places_orders(tmp_path):
    class Tripwire(SimBroker):
        armed = False

        def market_order(self, *a, **k):
            assert not self.armed, "learning placed an order"
            return super().market_order(*a, **k)

        close = modify = lambda self, *a, **k: (_ for _ in ()).throw(AssertionError("learning touched a position"))

    sim = Tripwire(seed=4, start=MON_08, history_days=5)
    h = LH(tmp_path, sim=sim)
    await h.mgr.tick_once()
    s = await h.session()
    await h.mgr.start(s.id)
    await h.mgr.pause(s.id)  # paused: the engine opens nothing, learning still Shadow Trades
    sim.armed = True
    await h.bars(120)
    await h.L.drain()
    champ = h.L.champion_of(h.assignment(s))
    assert len(h.L.repo.shadow_closed("EURUSD", "M1", champ.key)) > 3
    assert sim.positions() == []
    await h.close()


# ------------------------------------------------------------------ optimizer
async def test_first_start_queues_an_optimizer_run(lh):
    s = await lh.session(strategy="rsi_reversion", params={})
    await lh.mgr.start(s.id)
    await lh.L.drain()
    [run] = lh.L.repo.runs("EURUSD", "M1")
    assert run.trigger == "first_start" and run.status == "done", run.note
    assert run.result["finalists"] and run.result["champion"]["walk_forward"]["oos"]["n"] >= 0
    assert any("Optimizer EURUSD M1" in j.message for j in lh.store.journal(limit=100))


async def test_duplicate_triggers_are_merged(lh):
    s = await lh.session()
    a = lh.assignment(s)
    r1 = lh.L.request_optimize(a.symbol, a.timeframe, "manual")
    r2 = lh.L.request_optimize(a.symbol, a.timeframe, "daily")
    assert r1 == r2
    await lh.L.drain()


async def test_install_keeps_at_most_three_challengers(lh):
    s = await lh.session()

    def momentum(n=3000, seed=1):
        rng = np.random.default_rng(seed)
        r = np.zeros(n)
        e = rng.standard_normal(n) * 0.0007
        for i in range(1, n):
            r[i] = 0.3 * r[i - 1] + e[i]
        c = 1.1 * np.exp(np.cumsum(r))
        o = np.r_[c[0], c[:-1]]
        return pd.DataFrame({"time": MON_08 + np.arange(n) * 60, "open": o, "high": np.maximum(o, c) * 1.0002, "low": np.minimum(o, c) * 0.9998, "close": c, "volume": 1.0})

    res = optimize(momentum(), Candidate.of("rsi_reversion", {}), Costs(0.00012), n_candidates=60)
    assert res.picks
    lh.L._install("EURUSD", "M1", 1, res, MON_08)
    lh.L._install("EURUSD", "M1", 2, res, MON_08)  # second install: nothing mature to replace, stays ≤ 3
    assert 1 <= len(lh.L.repo.challengers("EURUSD", "M1")) <= 3


# ------------------------------------------------------------ promotion/rollback
async def test_manual_promotion_needs_guardrails_or_override_and_waits_until_flat(lh):
    s = await lh.session()
    await lh.mgr.start(s.id)
    challenger = Candidate.of("ema_cross", {"fast": 3, "slow": 7, "atr_period": 5, "sl_atr": 3, "tp_atr": 6})
    ch = add_challenger(lh, challenger)
    with pytest.raises(DomainError) as e:
        lh.L.promote(ch.id, s.id)
    assert e.value.code == "guardrails_failed" and "Shadow Trades" in e.value.message
    lh.L.promote(ch.id, s.id, force=True)
    assert lh.L.pending(s.id, "EURUSD").kind == "promotion"
    a_id = lh.assignment(s).id
    for _ in range(200):  # applied at the first bar close where the Assignment is flat
        await lh.bars(1)
        if lh.L.pending(s.id, "EURUSD") is None:
            break
    a = lh.assignment(s)
    assert a.id == a_id and Candidate.of(a.strategy, a.params).key == challenger.key
    kinds = [v.kind for v in lh.L.repo.versions(s.id, "EURUSD")]
    assert kinds == ["initial", "promotion"]
    notes = [c.note for c in lh.L.repo.challengers("EURUSD", "M1")]
    assert "previous champion" in notes  # old champion keeps shadow-trading as evidence
    assert "promotion" in lh.kinds(s.id)

    lh.L.rollback(s.id, "EURUSD")
    for _ in range(200):
        await lh.bars(1)
        if lh.L.pending(s.id, "EURUSD") is None:
            break
    a = lh.assignment(s)
    assert Candidate.of(a.strategy, a.params).key == Candidate.of("ema_cross", FAST).key
    assert [v.kind for v in lh.L.repo.versions(s.id, "EURUSD")][-1] == "rollback"


async def test_auto_promotion_when_all_guardrails_pass(lh):
    s = await lh.session()
    await lh.mgr.start(s.id)
    await lh.bars(2)
    champ = lh.L.champion_of(lh.assignment(s))
    better = Candidate.of("donchian_breakout", {"period": 12})
    ch = add_challenger(lh, better, started=MON_08)
    add_shadows(lh, better.key, [1.2, -1, 1.5, 0.8, -1, 1.1] * 7, MON_08)
    add_shadows(lh, champ.key, [0.2, -1, 0.5, -1] * 9, MON_08)
    assert lh.L.challenger_report(ch, champ)["promotable"]
    lh.L.set_auto_promote(s.id, "EURUSD", True, is_live=False)
    for _ in range(200):
        await lh.bars(1)
        vs = lh.L.repo.versions(s.id, "EURUSD")
        if vs[-1].kind == "auto_promotion":
            break
    assert lh.L.repo.versions(s.id, "EURUSD")[-1].kind == "auto_promotion"
    a = lh.assignment(s)
    assert a.strategy == "donchian_breakout"


async def test_no_auto_promotion_on_a_live_account(tmp_path):
    from .conftest import LiveSim

    h = LH(tmp_path, sim=LiveSim(seed=9, start=MON_08, history_days=5))
    await h.mgr.tick_once()
    h.store.set_live_enabled(h.sim.account().login, True)
    s = await h.session()
    await h.mgr.start(s.id)
    with pytest.raises(DomainError):
        h.L.set_auto_promote(s.id, "EURUSD", True, is_live=True)
    # even if the flag were set and an automatic change queued, the engine refuses it on a live account
    h.L.repo.set_auto_promote(s.id, "EURUSD", True)
    champ = h.L.champion_of(h.assignment(s))
    better = Candidate.of("donchian_breakout", {"period": 12})
    add_challenger(h, better, started=MON_08)
    add_shadows(h, better.key, [1.2, -1, 1.5, 0.8, -1, 1.1] * 7, MON_08)
    add_shadows(h, champ.key, [0.2, -1, 0.5, -1] * 9, MON_08)
    await h.bars(5)
    assert h.L.pending(s.id, "EURUSD") is None  # never queued (is_demo False)
    h.L.repo.add_pending(session_id=s.id, symbol="EURUSD", candidate_key=better.key, kind="auto_promotion")
    await h.bars(60)
    assert h.assignment(s).strategy == "ema_cross"
    assert all(v.kind != "auto_promotion" for v in h.L.repo.versions(s.id, "EURUSD"))
    await h.close()


async def test_auto_rollback_after_bad_live_run(lh):
    s = await lh.session()
    await lh.mgr.start(s.id)
    old, new = Candidate.of("ema_cross", FAST), Candidate.of("donchian_breakout", {"period": 12})
    for c in (old, new):
        lh.L.repo.ensure_candidate(c)
    lh.L.repo.add_version(session_id=s.id, symbol="EURUSD", timeframe="M1", candidate_key=new.key, previous_key=old.key, kind="auto_promotion", server_ts=MON_08)
    add_shadows(lh, old.key, [0.5, -1, 1.0] * 5, MON_08)  # previous champion keeps doing fine in shadow
    from fxcommand.store import TradeRow

    for i in range(20):
        rec = lh.L.repo.add_signal(session_id=s.id, symbol="EURUSD", timeframe="M1", strategy="donchian_breakout", candidate_key=new.key, ts=MON_08 + 60 * i, side="long", ticket=900_000 + i)
        tr = TradeRow(ticket=900_000 + i, session_id=s.id, magic=s.magic, symbol="EURUSD", side="long", volume=0.1, open_time=MON_08, open_price=1, status="closed", profit=-50.0, risk_amount=50.0)
        lh.L.on_live_close(tr, s)
        assert rec.id
    p = lh.L.pending(s.id, "EURUSD")
    assert p is not None and p.kind == "auto_rollback" and p.candidate_key == old.key


# ------------------------------------------------------------------ filter
async def test_active_filter_blocks_live_entries_but_shadow_continues(lh):
    s = await lh.session()
    blocking = sf.FilterState(mode="active", samples=100, weights=[0.0] * 12, bias=0.0, mean=[0.0] * 12, std=[1.0] * 12, threshold=0.99)
    lh.L.repo.save_filter(filter_key("EURUSD", "M1", "ema_cross"), blocking.to_dict(), 10_000)
    await lh.mgr.start(s.id)
    await lh.bars(80)
    assert "filter" in lh.kinds(s.id) and "order" not in lh.kinds(s.id)
    assert lh.sim.positions(s.magic) == []
    champ = lh.L.champion_of(lh.assignment(s))
    assert lh.L.repo.shadow_closed("EURUSD", "M1", champ.key)  # counterfactual evidence keeps accruing
    assert {r.decision for r in lh.L.repo.signals("EURUSD", "M1")} == {"blocked"}


async def test_filter_learns_from_shadow_trades(lh):
    s = await lh.session()
    await lh.mgr.start(s.id)
    await lh.bars(300)
    row = lh.L.repo.filter_state(filter_key("EURUSD", "M1", "ema_cross"))
    assert row is not None and row.trained_on >= 10
    assert row.state["mode"] in ("observe", "no_edge", "active", "disabled")


# ------------------------------------------------------------------ safety
async def test_learning_failure_never_stops_trading(lh):
    s = await lh.session()

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    lh.L.on_bar = boom
    lh.L.screen = boom
    await lh.mgr.start(s.id)
    await lh.bars(60)
    assert "order" in lh.kinds(s.id)  # trading carried on
    errors = [j for j in lh.store.journal(kinds=["learning"], limit=100) if "kaboom" in j.message]
    assert len(errors) == 1  # journaled once, not every bar
    assert lh.mgr.status.connected


async def test_sim_restart_voids_open_shadow_trades(lh):
    lh.L.repo.add_shadow(symbol="EURUSD", timeframe="M1", candidate_key="x", side="long", signal_ts=1, open_ts=2, entry=1.0)
    L2 = LearningService(lh.store, lh.thread, lh.journal, "sim")
    L2.start()
    assert lh.L.repo.open_shadows("EURUSD", "M1") == []
    await L2.stop()


async def test_noise_market_never_promotes(tmp_path):
    """Random-walk market, auto-promotion ON, optimizer running: nothing may be promoted."""
    h = LH(tmp_path, sim=SimBroker(seed=21, start=MON_08, history_days=5))
    await h.mgr.tick_once()
    s = await h.session(strategy="rsi_reversion", params={})
    await h.mgr.start(s.id)
    h.L.set_auto_promote(s.id, "EURUSD", True, is_live=False)
    for _ in range(6):
        h.L.request_optimize("EURUSD", "M1", "manual")
        await h.L.drain()
        await h.bars(200)
    kinds = [v.kind for v in h.L.repo.versions(s.id, "EURUSD")]
    assert "auto_promotion" not in kinds, kinds
    assert h.L.pending(s.id, "EURUSD") is None
    await h.close()


async def test_overview_and_arena_views(lh):
    s = await lh.session()
    await lh.mgr.start(s.id)
    await lh.bars(40)
    await lh.L.drain()
    ov = lh.L.overview(is_live=False)
    [arena] = ov["arenas"]
    assert arena["symbol"] == "EURUSD" and arena["session"]["id"] == s.id and arena["champion"]["label"]
    detail = lh.L.arena("EURUSD", "M1", is_live=False)
    assert detail["history"][0]["kind"] == "initial" and detail["runs"]
    assert arena["champion"]["key"] in detail["curves"]


async def test_promotion_applies_even_for_an_always_in_market_champion(lh):
    """EMA 2/3 with reverse-on-opposite is never flat at a bar boundary; the Promotion must take over
    at the moment the old Champion closes its position instead of waiting forever."""
    s = await lh.session()
    await lh.mgr.start(s.id)
    for _ in range(100):
        await lh.bars(1)
        if lh.sim.positions(s.magic):
            break
    challenger = Candidate.of("donchian_breakout", {"period": 8, "exit_period": 4, "atr_period": 5, "sl_atr": 3, "tp_atr": 6})
    ch = add_challenger(lh, challenger)
    lh.L.promote(ch.id, s.id, force=True)
    for _ in range(40):
        await lh.bars(1)
        if lh.L.pending(s.id, "EURUSD") is None:
            break
    assert lh.assignment(s).strategy == "donchian_breakout"


async def test_genuine_edge_is_learned_and_auto_promoted_end_to_end(tmp_path):
    """No fabricated evidence: on a market with a planted momentum edge, the real chain —
    Optimizer Run → Challengers → Shadow Trades on live bars → Guardrails → auto-promotion —
    replaces a mean-reversion Champion with a trend-following Candidate."""
    from fxcommand.broker.sim import SimSymbolSpec

    trend = SimSymbolSpec("TREND", "Trending test market", "XXX", "USD", 1.2000, 5, 100_000, 4e-4, 5, stops_level=0)
    h = LH(tmp_path, sim=SimBroker(seed=3, start=MON_08, history_days=6, symbols=(trend,), momentum=0.3))
    await h.mgr.tick_once()
    h.store.update_app_settings({"learning_candidates": 60, "learning_bars": 4000})
    s = await h.session(symbols=("TREND",), strategy="rsi_reversion", params={})
    h.L.set_auto_promote(s.id, "TREND", True, is_live=False)
    await h.mgr.start(s.id)  # first start queues the first Optimizer Run
    await h.L.drain()
    assert h.L.repo.challengers("TREND", "M1"), "the optimizer should find the planted edge"
    for _ in range(15):
        await h.bars(100)
        if h.L.repo.versions(s.id, "TREND")[-1].kind == "auto_promotion":
            break
    versions = h.L.repo.versions(s.id, "TREND")
    assert versions[-1].kind == "auto_promotion", [v.kind for v in versions]
    assert h.assignment(s, "TREND").strategy != "rsi_reversion"
    assert "all Guardrails passed" in versions[-1].reason
    await h.close()


async def test_auto_rollback_applies_on_a_live_account(tmp_path):
    from fxcommand.store import TradeRow

    from .conftest import LiveSim

    h = LH(tmp_path, sim=LiveSim(seed=9, start=MON_08, history_days=5))
    await h.mgr.tick_once()
    h.store.set_live_enabled(h.sim.account().login, True)
    s = await h.session()
    await h.mgr.start(s.id)
    old, new = Candidate.of("ema_cross", FAST), Candidate.of("donchian_breakout", {"period": 12})
    for c in (old, new):
        h.L.repo.ensure_candidate(c)
    # the operator promoted manually; the new Champion then loses 20 live trades in a row
    h.L.repo.add_version(session_id=s.id, symbol="EURUSD", timeframe="M1", candidate_key=new.key, previous_key=old.key, kind="promotion", server_ts=MON_08)
    h.store.update_assignment(h.assignment(s).id, strategy=new.strategy, params=new.param_dict)
    add_shadows(h, old.key, [0.5, -1, 1.0] * 5, MON_08)
    for i in range(20):
        h.L.repo.add_signal(session_id=s.id, symbol="EURUSD", timeframe="M1", strategy=new.strategy, candidate_key=new.key, ts=MON_08 + 60 * i, side="long", ticket=800_000 + i)
        h.L.on_live_close(TradeRow(ticket=800_000 + i, session_id=s.id, magic=s.magic, symbol="EURUSD", side="long", volume=0.1, open_time=MON_08, open_price=1, status="closed", profit=-50.0, risk_amount=50.0), s)
    assert h.L.pending(s.id, "EURUSD").kind == "auto_rollback"
    for _ in range(100):
        await h.bars(1)
        if h.L.pending(s.id, "EURUSD") is None:
            break
    assert h.assignment(s).strategy == "ema_cross"
    assert h.L.repo.versions(s.id, "EURUSD")[-1].kind == "auto_rollback"
    await h.close()
