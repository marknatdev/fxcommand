"""Pure learning layer: Candidate identity, Shadow Trade rules, objective, Guardrails, Signal Filter,
and the two calibration tests that matter most — noise must never look promotable, a planted edge must."""

import numpy as np
import pandas as pd
import pytest

from fxcommand.learning import signal_filter as sf
from fxcommand.learning.candidate import Candidate, generate, perturb
from fxcommand.learning.objective import deflation, guardrails, r_stats, should_rollback, walk_forward
from fxcommand.learning.optimizer import optimize
from fxcommand.learning.paper import Costs, ExitRules, PaperTrader, PaperTrade, backtest

T0 = 1_704_700_800
NO = {"long": False, "short": False, "exit_long": False, "exit_short": False, "sl_dist": 0.01, "tp_dist": 0.02}


def sig(**kw):
    return {**NO, **kw}


def series(n, seed, phi=0.0, vol=0.0007):
    """Synthetic 15-minute bars: phi > 0 plants momentum (trend-following edge), phi = 0 is pure noise."""
    rng = np.random.default_rng(seed)
    e = rng.standard_normal(n) * vol
    r = np.zeros(n)
    for i in range(1, n):
        r[i] = phi * r[i - 1] + e[i]
    c = 1.1 * np.exp(np.cumsum(r))
    o = np.r_[c[0], c[:-1]]
    w = np.abs(rng.standard_normal((2, n))) * vol * 0.5
    return pd.DataFrame(
        {"time": T0 + np.arange(n) * 900, "open": o, "high": np.maximum(o, c) * (1 + w[0]), "low": np.minimum(o, c) * (1 - w[1]), "close": c, "volume": 100.0}
    )


# ------------------------------------------------------------------ Candidate
def test_candidate_identity_is_canonical():
    a = Candidate.of("ema_cross", {"fast": 5, "slow": 13})
    b = Candidate.of("ema_cross", {"slow": 13.0, "fast": 5.0, "bogus": 1})
    assert a.key == b.key and a == b
    assert Candidate.of("ema_cross", {"fast": 6, "slow": 13}).key != a.key
    assert "fast=5" in a.label()


def test_generation_is_distinct_valid_and_mixes_families():
    rng = np.random.default_rng(1)
    champ = Candidate.of("ema_cross", {})
    cands = generate(champ, 120, rng)
    assert len({c.key for c in cands}) == len(cands) == 120 and champ.key not in {c.key for c in cands}
    assert {c.strategy for c in cands} == {"ema_cross", "donchian_breakout", "rsi_reversion"}
    for c in cands:
        p = c.param_dict
        if c.strategy == "ema_cross":
            assert p["fast"] < p["slow"]
        if c.strategy == "rsi_reversion":
            assert p["oversold"] < p["overbought"]
    n = perturb(champ, rng, 0.1)
    assert n.strategy == "ema_cross" and n.key != champ.key


# ---------------------------------------------------------------- PaperTrader
def trader(**rules):
    return PaperTrader(Costs(spread=0.0002, slippage=0.0), ExitRules(**rules))


def test_entry_next_open_at_ask_and_stop_loss_is_minus_one_r():
    tr = trader()
    tr.on_bar(T0, 1.0, 1.0, 1.0, 1.0, 0.001, sig(long=True, sl_dist=0.01, tp_dist=0.02))
    assert tr.position is None  # nothing fills on the signal bar
    opened, closed = tr.on_bar(T0 + 60, 1.0, 1.001, 0.999, 1.0, 0.001, sig())
    assert opened.entry == pytest.approx(1.0002) and opened.sl == pytest.approx(0.9902)
    _, closed = tr.on_bar(T0 + 120, 1.0, 1.0, 0.98, 0.99, 0.001, sig())
    assert closed[0].reason == "sl" and closed[0].r == pytest.approx(-1.0)


def test_take_profit_and_both_touched_means_stop_first():
    tr = trader()
    tr.on_bar(T0, 1, 1, 1, 1, 0.001, sig(long=True))
    tr.on_bar(T0 + 60, 1, 1, 1, 1, 0.001, sig())
    _, closed = tr.on_bar(T0 + 120, 1.0, 1.03, 0.999, 1.02, 0.001, sig())
    assert closed[0].reason == "tp" and closed[0].r == pytest.approx(2.0)
    tr2 = trader()
    tr2.on_bar(T0, 1, 1, 1, 1, 0.001, sig(long=True))
    tr2.on_bar(T0 + 60, 1, 1, 1, 1, 0.001, sig())
    _, closed = tr2.on_bar(T0 + 120, 1.0, 1.05, 0.95, 1.0, 0.001, sig())
    assert closed[0].reason == "sl"


def test_gap_through_stop_fills_at_open():
    tr = trader()
    tr.on_bar(T0, 1, 1, 1, 1, 0.001, sig(short=True))
    tr.on_bar(T0 + 60, 1, 1, 1, 1, 0.001, sig())
    _, closed = tr.on_bar(T0 + 120, 1.05, 1.06, 1.04, 1.05, 0.001, sig())
    assert closed[0].reason == "sl" and closed[0].r < -4  # far worse than -1R


def test_signal_bar_range_never_triggers_the_new_position():
    tr = trader()
    tr.on_bar(T0, 1.0, 1.0, 0.5, 1.0, 0.001, sig(long=True))  # signal bar dips far below the future stop
    opened, closed = tr.on_bar(T0 + 60, 1.0, 1.001, 0.999, 1.0, 0.001, sig())
    assert opened and not closed


def test_exit_signal_and_reverse_rules():
    tr = trader()
    tr.on_bar(T0, 1, 1, 1, 1, 0.001, sig(long=True))
    tr.on_bar(T0 + 60, 1, 1.001, 0.999, 1, 0.001, sig(exit_long=True, short=True))  # exit wins, no entry
    _, closed = tr.on_bar(T0 + 120, 1.001, 1.002, 1.0, 1.001, 0.001, sig())
    assert closed[0].reason == "exit" and tr.position is None

    tr = trader()
    tr.on_bar(T0, 1, 1, 1, 1, 0.001, sig(long=True))
    tr.on_bar(T0 + 60, 1, 1.001, 0.999, 1, 0.001, sig(short=True))
    opened, closed = tr.on_bar(T0 + 120, 1, 1.001, 0.999, 1, 0.001, sig())
    assert closed[0].reason == "reverse" and opened.side == "short"

    tr = trader(reverse_on_opposite=False)
    tr.on_bar(T0, 1, 1, 1, 1, 0.001, sig(long=True))
    tr.on_bar(T0 + 60, 1, 1.001, 0.999, 1, 0.001, sig(short=True))
    opened, closed = tr.on_bar(T0 + 120, 1, 1.001, 0.999, 1, 0.001, sig())
    assert closed[0].reason == "reverse" and opened is None


def test_breakeven_then_stopped_at_entry_is_zero_r():
    tr = trader(breakeven=True, breakeven_at_r=1.0)
    tr.on_bar(T0, 1, 1, 1, 1, 0.001, sig(long=True, tp_dist=0))
    tr.on_bar(T0 + 60, 1, 1.02, 0.999, 1.015, 0.001, sig())  # +1.3R at the close -> SL to entry
    assert tr.position.sl == pytest.approx(tr.position.entry)
    _, closed = tr.on_bar(T0 + 120, 1.01, 1.01, 0.99, 0.99, 0.001, sig())
    assert closed[0].r == pytest.approx(0.0, abs=1e-9)


def test_allow_entry_blocks_and_snapshot_restores():
    tr = PaperTrader(Costs(0.0002), ExitRules(), allow_entry=lambda t: False)
    tr.on_bar(T0, 1, 1, 1, 1, 0.001, sig(long=True))
    opened, _ = tr.on_bar(T0 + 60, 1, 1, 1, 1, 0.001, sig())
    assert opened is None
    tr = trader()
    tr.on_bar(T0, 1, 1, 1, 1, 0.001, sig(long=True))
    tr.on_bar(T0 + 60, 1, 1, 1, 1, 0.001, sig())
    snap = tr.snapshot()
    tr2 = trader()
    tr2.restore(snap)
    assert tr2.position.entry == tr.position.entry


def test_backtest_matches_manual_replay_and_is_deterministic():
    bars = series(800, 3, 0.2)
    c = Candidate.of("donchian_breakout", {"period": 12, "exit_period": 5})
    a = backtest(bars, c, Costs(0.0001))
    b = backtest(bars, c, Costs(0.0001))
    assert a == b and len(a) > 5
    assert all(t.open_time > t.signal_time for t in a)  # entries never on the signal bar


# ------------------------------------------------------------------ objective
def test_r_stats_and_drawdown():
    s = r_stats([1, -1, -1, 2, -1])
    assert s.n == 5 and s.mean == pytest.approx(0.0) and s.max_dd == pytest.approx(2.0) and s.win_rate == pytest.approx(0.4)
    assert r_stats([]).n == 0


def test_walk_forward_splits_by_open_time():
    trades = [PaperTrade("long", T0 + i, T0 + i + 1, T0 + i - 1, 1, 1, r, "tp") for i, r in enumerate([1, 1, -1, 2, -1, 1])]
    wf = walk_forward(trades, T0 + 3, [T0 + 5])
    assert wf.in_sample.n == 3 and wf.oos.n == 3 and [f.n for f in wf.folds] == [2, 1]


def test_guardrails_table():
    good = r_stats([0.8, -1, 1.5, 0.6, -0.5] * 8)
    champ = r_stats([0.2, -1, 0.5, -0.3] * 8)
    oos_good, oos_champ = r_stats([0.6, -1, 1.2, 0.8] * 15), r_stats([0.1, -1, 0.9] * 10)
    checks = {c.name: c.ok for c in guardrails(good, champ, oos_good, oos_champ, 10, True)}
    assert all(checks.values()), checks
    few = {c.name: c.ok for c in guardrails(r_stats([1.0] * 5), champ, oos_good, oos_champ, 10, True)}
    assert not few["shadow_trades"]
    assert not {c.name: c.ok for c in guardrails(good, champ, None, None, 10, True)}["walk_forward"]
    assert not {c.name: c.ok for c in guardrails(good, champ, oos_good, oos_champ, 10, False)}["robust"]
    assert deflation(10) == pytest.approx(0.5 * np.sqrt(2 * np.log(10)))


def test_rollback_rule():
    assert not should_rollback([-1.0] * 19, 0.2)
    assert should_rollback([-0.5] * 20, 0.2)
    assert not should_rollback([0.1] * 20, 0.2)


# -------------------------------------------------------------- Signal Filter
def _filter_samples(n, seed, edge):
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        x = rng.standard_normal(12).tolist()
        p_win = 1 / (1 + np.exp(-2.5 * x[3])) if edge else 0.45
        out.append((x, 1.5 if rng.random() < p_win else -1.0))
    return out


def test_filter_observes_until_enough_trades():
    st = sf.train(_filter_samples(30, 0, True))
    assert st.mode == "observe" and not sf.blocks(st, 0.01)


def test_filter_learns_planted_edge():
    st = sf.train(_filter_samples(300, 1, True))
    assert st.mode == "active", st.note
    assert st.lift_r > 0.1
    assert int(np.argmax(np.abs(st.weights))) == 3 and st.weights[3] > 0  # found the feature carrying the edge
    assert sf.blocks(st, sf.predict(st, [0.0] * 3 + [-2.0] + [0.0] * 8))
    assert not sf.blocks(st, sf.predict(st, [0.0] * 3 + [2.0] + [0.0] * 8))


@pytest.mark.parametrize("seed", range(40))
def test_filter_never_activates_on_noise(seed):
    st = sf.train(_filter_samples(200, 100 + seed, False))
    assert st.mode in ("no_edge", "observe"), st.note


def test_filter_disables_itself_when_blocked_signals_do_better():
    st = sf.train(_filter_samples(300, 1, True))
    recent = [(0.1, 2.0)] * 12 + [(0.9, -1.0)] * 12  # blocked ones won, kept ones lost
    off = sf.review(st, recent)
    assert off.mode == "disabled" and "disabled" in off.note
    assert sf.train(_filter_samples(300, 2, True), previous=off).mode == "disabled"  # stays off


# ------------------------------------------------------ optimizer calibration
CHAMP = Candidate.of("rsi_reversion", {})
COSTS = Costs(spread=0.00012, slippage=0.00001)


def _wf_passes(res):
    if not res.picks:
        return False
    best = res.picks[0]
    checks = guardrails(best.wf.oos, r_stats([]), best.wf.oos, res.champion.wf.oos, res.trials, best.robust)
    return next(c for c in checks if c.name == "walk_forward").ok


@pytest.mark.parametrize("seed", range(4))
def test_noise_market_is_never_promotable(seed):
    res = optimize(series(4000, seed), CHAMP, COSTS, n_candidates=120, seed=seed)
    assert not _wf_passes(res), [s.wf.oos.sqn for s in res.picks]


def test_planted_momentum_edge_is_found():
    passed = 0
    for seed in range(3):
        res = optimize(series(4000, seed, phi=0.3), CHAMP, COSTS, n_candidates=120, seed=seed)
        passed += _wf_passes(res)
        assert res.picks and res.picks[0].wf.oos.sqn > res.champion.wf.oos.sqn
    assert passed >= 2


def test_insufficient_history_is_reported():
    res = optimize(series(300, 0), CHAMP, COSTS)
    assert res.insufficient and "300 bars" in res.insufficient and res.picks == []
