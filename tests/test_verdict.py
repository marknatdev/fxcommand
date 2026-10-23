"""T8: each Prosecutor red flag, the Pass Bar, the Verdict and the Closest Miss."""

import math
from types import SimpleNamespace

import numpy as np
import pytest
import test_baseline as tb

from vibedesk.blocks.spec import expand_grid, validate
from vibedesk.engine.simulate import TRADE_DTYPE
from vibedesk.gauntlet.prosecutor import (
    concentration,
    neighbour_indexes,
    neighbours,
    prosecute,
    regimes,
    sides,
)
from vibedesk.gauntlet.verdict import (
    CANDIDATE,
    FAIL,
    NA,
    PASS,
    REJECT,
    Check,
    decide,
    judged,
    pass_bar,
)
from vibedesk.market.bars import Bars, Timeframe


def trades(r, side=1, entry_idx=None):
    out = np.zeros(len(r), dtype=TRADE_DTYPE)
    out["r"] = r
    out["side"] = side
    out["entry_idx"] = np.arange(1, len(r) + 1) if entry_idx is None else entry_idx
    return out


def wins_and_losses(wins, losses, win=2.0, loss=-1.0):
    return [win] * wins + [loss] * losses


# The Prosecutor -------------------------------------------------------------------------------


@pytest.mark.parametrize("share, status", [(0.6, FAIL), (0.5, PASS), (0.3, PASS), (math.nan, NA)])
def test_concentration(share, status):
    assert concentration(share).status == status


GRID = {
    "name": "grid",
    "symbol": "GOLD",
    "timeframe": "M15",
    "direction": "both",
    "entry": {"block": "donchian_breakout", "params": {"period": [10, 20, 30]}},
    "exit": {"stop": {"block": "atr_stop", "params": {"mult": [1.0, 2.0]}}},
    "sizing": {"block": "fixed_risk", "params": {}},
}


@pytest.fixture
def grid():
    combinations = expand_grid(validate(GRID))
    index = {(c.entry.params.period, c.stop.params.mult): i for i, c in enumerate(combinations)}
    return combinations, index


def test_neighbours_are_one_step_on_one_axis(grid):
    combinations, index = grid

    found = neighbour_indexes(combinations, index[(20, 1.0)])

    assert sorted(found) == sorted([index[(10, 1.0)], index[(30, 1.0)], index[(20, 2.0)]])


def test_weak_neighbours_raise_a_red_flag_and_strong_ones_do_not(grid):
    combinations, index = grid
    pf = np.full(len(combinations), 3.0)  # far-away Combinations don't count
    pf[index[(20, 1.0)]] = 2.5
    pf[[index[(10, 1.0)], index[(30, 1.0)], index[(20, 2.0)]]] = [0.8, 0.9, 1.5]

    weak = neighbours(combinations, pf, index[(20, 1.0)])
    pf[index[(10, 1.0)]] = 1.1
    strong = neighbours(combinations, pf, index[(20, 1.0)])

    assert (weak.status, weak.value) == (FAIL, 0.9)
    assert (strong.status, strong.value) == (PASS, 1.1)


def test_neighbours_without_a_choice_or_traded_neighbours_are_na(grid):
    combinations, index = grid
    pf = np.full(len(combinations), np.nan)
    pf[index[(10, 2.0)]] = 0.2  # two steps away

    assert neighbours(combinations, pf, None).status == NA
    assert neighbours(combinations, pf, index[(20, 1.0)]).status == NA


def test_a_losing_side_raises_a_red_flag():
    record = np.concatenate(
        [trades(wins_and_losses(20, 20), side=1), trades(wins_and_losses(12, 28), side=-1)]
    )

    long, short = sides(record, "both")

    assert (long.status, long.value) == (PASS, 2.0)
    assert short.status == FAIL
    assert short.value == pytest.approx(24 / 28)


def test_sides_need_thirty_trades_and_a_two_way_spec():
    record = np.concatenate(
        [trades(wins_and_losses(20, 20), side=1), trades(wins_and_losses(2, 27), side=-1)]
    )

    long, short = sides(record, "both")

    assert long.status == PASS
    assert short.status == NA  # 29 shorts: too thin to judge, never a pass
    assert sides(record, "long") == []


def regime_bars(n=400):
    # Every bar's range grows with time, so ATR as a share of price grows with the bar index.
    close = np.full(n, 100.0)
    half = np.linspace(0.1, 3.0, n)
    return Bars("GOLD", Timeframe.M15, 0.01, 2, 900 * np.arange(n), close, close + half,
                close - half, close, np.zeros(n))  # fmt: skip


def test_a_losing_volatility_regime_raises_a_red_flag():
    # 90 trades spread over the bars: the quiet third wins, the normal third breaks even and
    # the busy third loses.
    entry = np.linspace(30, 390, 90).astype(np.int64)
    r = wins_and_losses(20, 10) + wins_and_losses(10, 20) + wins_and_losses(5, 25)

    quiet, normal, busy = regimes(trades(r, entry_idx=entry), regime_bars())

    assert (quiet.status, quiet.value, quiet.note) == (PASS, 4.0, "30 trades")
    assert (normal.status, normal.value) == (PASS, 1.0)
    assert busy.status == FAIL
    assert busy.value == pytest.approx(10 / 25)


def test_regimes_with_too_few_trades_are_na():
    entry = np.linspace(30, 390, 60).astype(np.int64)

    checks = regimes(trades([1.0, -1.0] * 30, entry_idx=entry), regime_bars())

    assert [c.status for c in checks] == [NA, NA, NA]


def test_prosecute_on_a_walk_forward():
    wf = tb.walk(3)

    checks = prosecute(wf)

    assert [c.name for c in checks] == [
        "top5_share",
        "neighbour_profit_factor",
        "long_profit_factor",
        "short_profit_factor",
        "quiet_regime_profit_factor",
        "normal_regime_profit_factor",
        "busy_regime_profit_factor",
    ]
    assert {c.status for c in checks} <= {PASS, FAIL, NA}


# The Pass Bar and the Verdict ------------------------------------------------------------------


def walk(pf=1.5, trades=150, folds=(1, 2, 1, 3, -1, 2, 1, 1), stress_pf=1.2):
    return SimpleNamespace(
        metrics=SimpleNamespace(profit_factor=pf, trades=trades),
        fold_net_r=np.array(folds, dtype=float),
        stress_metrics=SimpleNamespace(profit_factor=stress_pf),
    )


def baseline(net=50.0, p95=20.0, percentile=0.99):
    return SimpleNamespace(strategy_net_r=net, p95=p95, percentile=percentile)


def test_a_clean_walk_forward_is_a_candidate():
    checks = pass_bar(walk(), baseline(), deflated_sharpe=0.97, pbo=0.1)

    verdict = decide(checks)

    assert [c.name for c in checks] == [
        "profit_factor",
        "trades",
        "positive_folds",
        "stress_profit_factor",
        "coin_flip_baseline",
        "deflated_sharpe",
        "pbo",
    ]
    assert verdict.verdict == CANDIDATE
    assert verdict.closest_miss is None


@pytest.mark.parametrize(
    "change, failed",
    [
        (dict(wf=walk(pf=1.19)), "profit_factor"),
        (dict(wf=walk(trades=99)), "trades"),
        (dict(wf=walk(folds=(1, 1, 1, 1, -1, -1, -1, -1))), "positive_folds"),
        (dict(wf=walk(stress_pf=1.04)), "stress_profit_factor"),
        (dict(base=baseline(net=20.0)), "coin_flip_baseline"),  # equal is not above
        (dict(dsr=0.94), "deflated_sharpe"),
        (dict(pbo=0.31), "pbo"),
    ],
)
def test_any_single_failed_check_rejects(change, failed):
    checks = pass_bar(
        change.get("wf", walk()),
        change.get("base", baseline()),
        change.get("dsr", 0.97),
        change.get("pbo", 0.1),
    )

    verdict = decide(checks)

    assert verdict.verdict == REJECT
    assert [c.name for c in verdict.failed()] == [failed]
    assert verdict.closest_miss["check"] == failed


def test_positive_folds_scale_with_the_number_of_folds():
    two = pass_bar(walk(folds=(1, -1)), baseline(), 0.97, 0.1)[2]
    eight = pass_bar(walk(), baseline(), 0.97, 0.1)[2]

    assert (two.threshold, two.status) == (2, FAIL)
    assert (eight.threshold, eight.value, eight.status) == (5, 7, PASS)


def test_a_year_that_trades_nothing_is_not_a_positive_fold():
    # A fold with no eligible Combination trades nothing and nets 0R: no profitable year, so a
    # strategy that trades in only half its years can't pass on the empty ones.
    check = pass_bar(walk(folds=(1, 2, 1, 3, 0, 0, 0, 0)), baseline(), 0.97, 0.1)[2]

    assert (check.value, check.threshold, check.status) == (4, 5, FAIL)


def test_pbo_may_be_na_but_the_core_checks_may_not():
    na_pbo = pass_bar(walk(), baseline(), 0.97, math.nan)
    na_core = pass_bar(walk(pf=math.nan), baseline(), math.nan, 0.1)

    assert decide(na_pbo).verdict == CANDIDATE
    assert na_pbo[-1].status == NA
    assert decide(na_core).verdict == REJECT
    assert {c.name for c in decide(na_core).failed()} == {"profit_factor", "deflated_sharpe"}


def test_an_na_prosecutor_check_does_not_fail_and_a_red_flag_does():
    core = pass_bar(walk(), baseline(), 0.97, 0.1)
    thin = Check("short_profit_factor", math.nan, 1.0, False, NA, "12 trades")
    flag = judged("top5_share", 0.7, 0.5, at_most=True)

    assert decide([*core, thin]).verdict == CANDIDATE
    assert decide([*core, thin, flag]).verdict == REJECT


def test_the_closest_miss_is_the_ratio_nearest_one():
    checks = [
        judged("profit_factor", 1.1, 1.2),  # 0.917
        judged("deflated_sharpe", 0.5, 0.95),  # 0.526
        judged("pbo", 0.32, 0.3, at_most=True),  # 0.9375, direction-aware: nearest
    ]

    miss = decide(checks).closest_miss

    assert miss == {"check": "pbo", "value": 0.32, "threshold": 0.3}


def test_a_prosecutor_flag_is_the_closest_miss_only_when_the_core_passes():
    core_fail = judged("profit_factor", 0.8, 1.2)  # 0.667
    flag = judged("busy_regime_profit_factor", 0.88, 0.9)  # 0.978, nearer but secondary
    clean_core = pass_bar(walk(), baseline(), 0.97, 0.1)

    assert decide([core_fail, flag]).closest_miss["check"] == "profit_factor"
    assert decide([*clean_core, flag]).closest_miss["check"] == "busy_regime_profit_factor"


def test_an_uncomputable_failure_is_never_the_closest_miss():
    checks = [judged("deflated_sharpe", math.nan, 0.95), judged("profit_factor", 0.6, 1.2)]

    assert decide(checks).closest_miss["check"] == "profit_factor"


def test_checks_serialise_without_nan():
    check = judged("deflated_sharpe", math.nan, 0.95)

    assert check.to_dict() == {
        "name": "deflated_sharpe",
        "value": None,
        "threshold": 0.95,
        "at_most": False,
        "status": FAIL,
        "note": "",
    }


def test_an_na_note_only_shows_when_the_check_does_not_apply():
    applies = pass_bar(walk(), baseline(), 0.97, 0.1)[-1]
    does_not = pass_bar(walk(), baseline(), 0.97, math.nan)[-1]

    assert (applies.status, applies.note) == (PASS, "")
    assert (does_not.status, does_not.note) == (NA, "a grid of one has nothing to overfit")
