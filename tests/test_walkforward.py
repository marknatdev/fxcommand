"""T6: walk-forward choices, the stitched blind record and spread stress."""

from datetime import UTC, date, datetime

import numpy as np
import pytest

from vibedesk.blocks.compile import compile_signals
from vibedesk.blocks.spec import expand_grid, validate
from vibedesk.engine.costs import CostSnapshot
from vibedesk.engine.metrics import metrics
from vibedesk.engine.simulate import TRADE_DTYPE, ExitReason, Trades, simulate
from vibedesk.gauntlet.stats import sharpe
from vibedesk.gauntlet.walkforward import Window, choose, stitch, walk_forward
from vibedesk.market.bars import Timeframe
from vibedesk.market.synthetic import SyntheticBarSource

M15 = 900


def epoch(*args) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp())


def records(trades) -> np.ndarray:
    """Trade records from (entry, exit, r[, reason]) tuples; bar indexes follow time order."""
    out = np.zeros(len(trades), dtype=TRADE_DTYPE)
    start = epoch(2014, 1, 1)
    for i, (entry, exit_, r, *reason) in enumerate(trades):
        out["entry_time"][i], out["exit_time"][i], out["r"][i] = entry, exit_, r
        out["entry_idx"][i], out["exit_idx"][i] = (entry - start) // M15, (exit_ - start) // M15
        out["exit_reason"][i] = reason[0] if reason else ExitReason.TARGET
    return out


def year_of(r_by_year):
    """Two-week trades in mid-year months, one per R value, for each year."""
    trades = []
    for year, values in sorted(r_by_year.items()):
        for month, r in enumerate(values, start=3):
            trades.append((epoch(year, month, 1), epoch(year, month, 15), r))
    return trades


# Per-year R of three Combinations, plus two traps that would flip a choice if they leaked:
# - C's +20R trade enters 2017-12-31 and exits in 2018, straddling the end of fold 2018's
#   training window.
# - A's +80R trade closes in 2018 only because the data ended (ExitReason.END).
# Training profit factors, worked by hand:
#   fold 2017, trains 2014-2016: A 6/4 = 1.5, B 4/4 = 1.0, C 1/5 = 0.2              -> A
#   fold 2018, trains 2015-2017: A 3/5 = 0.6, B 6/3 = 2.0, C 4/4 = 1.0              -> B
#     (with the straddler leaked, C would score 24/4 = 6.0 and win)
#   fold 2019, trains 2016-2018: A 0/6 = 0.0, B 4/4 = 1.0, C 27/3 = 9.0             -> C
#     (the straddler is inside this window; with the END close leaked, A would score
#     80/6 = 13.3 and win)
A = {2014: [3, -1], 2015: [3, -1], 2016: [-1, -1], 2017: [-1, -1], 2018: [-1, -1]}
B = {2014: [-1, -1], 2015: [2, -1], 2016: [2, -1], 2017: [2, -1], 2018: [-1, -1]}
C = {2014: [-1, -1], 2015: [-1, -1], 2016: [1, -1], 2017: [3, -1], 2018: [3, -1]}
STRADDLER = (epoch(2017, 12, 31, 12), epoch(2018, 1, 2, 9), 20.0)
END_CLOSE = (epoch(2018, 12, 20), epoch(2018, 12, 28), 80.0, ExitReason.END)


@pytest.fixture
def grid():
    a = records(sorted(year_of(A) + [END_CLOSE]))
    b = records(year_of(B))
    c = records(sorted(year_of(C) + [STRADDLER]))
    return [a, b, c]


@pytest.mark.parametrize("blind_year, expected", [(2017, 0), (2018, 1), (2019, 2)])
def test_choices_by_hand_and_nothing_leaks_across_the_window_end(grid, blind_year, expected):
    choice = choose(grid, Window.years(blind_year - 3, blind_year - 1), M15, min_trades=2)

    assert choice.chosen == expected


def test_training_profit_factors_by_hand(grid):
    choice = choose(grid, Window.years(2015, 2017), M15, min_trades=2)

    assert choice.profit_factor.tolist() == pytest.approx([0.6, 2.0, 1.0])
    assert choice.trades.tolist() == [6, 6, 6]  # the straddler and the END close are left out


def test_the_straddler_counts_once_it_lies_inside_a_window(grid):
    choice = choose(grid, Window.years(2016, 2018), M15, min_trades=2)

    assert choice.profit_factor[2] == pytest.approx((1 + 3 + 20 + 3) / 3)
    assert choice.profit_factor[0] == 0.0  # the END close is not counted


def test_an_exit_bar_that_closes_after_the_window_is_not_training_data():
    # A D1 bar opens at server midnight, 22:00 UTC the evening before, so the exit bar opening
    # 2016-12-31 22:00 UTC holds 22 hours of 2017 prices. It must not count for 2014-2016.
    d1 = 86400
    inside = records([(epoch(2016, 6, 1), epoch(2016, 12, 30, 22), 1.0)] * 3)
    late = records([(epoch(2016, 6, 1), epoch(2016, 12, 31, 22), 1.0)] * 3)

    choice = choose([inside, late], Window.years(2014, 2016), d1, min_trades=1)

    assert choice.trades.tolist() == [3, 0]


def test_too_few_trades_are_not_eligible_and_none_eligible_chooses_nothing(grid):
    few = records([(epoch(2016, 5, 1), epoch(2016, 5, 2), 9.0)] * 2)

    with_few = choose([few, *grid], Window.years(2014, 2016), M15, min_trades=6)
    nobody = choose(grid, Window.years(2014, 2016), M15, min_trades=7)

    assert with_few.chosen == 1  # A, not the two-trade wonder in front of it
    assert nobody.chosen is None
    assert nobody.trades.tolist() == [6, 6, 6]


def test_ties_go_to_the_earlier_combination(grid):
    copy = grid[1].copy()

    choice = choose([grid[1], copy], Window.years(2015, 2017), M15, min_trades=2)

    assert choice.chosen == 0


def test_stitch_drops_a_next_fold_trade_that_would_overlap(grid):
    # Fold 0 takes B's trades entered in 2017, fold 1 takes C's entered in 2018. A long trade
    # of B's from December 2017 into March 2018 overlaps C's first 2018 trade, which is dropped.
    long_b = records(year_of({2017: [2, -1]}) + [(epoch(2017, 12, 1), epoch(2018, 3, 10), 1.0)])
    windows = [Window.years(2017, 2017), Window.years(2018, 2018)]

    stitched, fold = stitch([long_b, grid[2]], [0, 1], windows)

    assert stitched["entry_time"].tolist() == [
        epoch(2017, 3, 1),
        epoch(2017, 4, 1),
        epoch(2017, 12, 1),
        epoch(2018, 4, 1),  # C's 2018-03-01 trade entered before B's trade exited
    ]
    assert fold.tolist() == [0, 0, 0, 1]


def test_a_reversal_survives_a_fold_boundary_only_within_one_simulation():
    # A long held into 2018 and its reversal entering at the very bar it left at: the same
    # Combination in both folds keeps the reversal; a different one may not trust that bar.
    held = records([(epoch(2017, 12, 1), epoch(2018, 1, 10), 1.0)])
    reversal = records([(epoch(2018, 1, 10), epoch(2018, 2, 1), 2.0)])
    same = np.concatenate([held, reversal])
    windows = [Window.years(2017, 2017), Window.years(2018, 2018)]

    kept, _ = stitch([same], [0, 0], windows)
    dropped, _ = stitch([held, reversal], [0, 1], windows)

    assert kept["r"].tolist() == [1.0, 2.0]
    assert dropped["r"].tolist() == [1.0]


def test_stitch_skips_folds_that_chose_nothing(grid):
    windows = [Window.years(y, y) for y in (2016, 2017, 2018)]

    stitched, fold = stitch(grid, [None, 1, None], windows)

    assert fold.tolist() == [1, 1]
    assert stitched["r"].tolist() == [2, -1]


# Walk-forward on synthetic bars ----------------------------------------------------------------

YEARS = (2017, 2018)
SPEC = {
    "name": "wf",
    "symbol": "GOLD",
    "timeframe": "H1",
    "direction": "both",
    "entry": {"block": "donchian_breakout", "params": {"period": [10, 30, 60]}},
    "exit": {
        "stop": {"block": "atr_stop", "params": {"mult": [1.0, 2.0]}},
        "target": {"block": "r_target", "params": {"r": 2.0}},
        "trailing": {"block": "atr_trailing", "params": {"mult": 3.0}},
    },
    "sizing": {"block": "fixed_risk", "params": {}},
}
COST = CostSnapshot(0.01, 55, 1, swap_long_points=-83.83, swap_short_points=20.64)


@pytest.fixture(scope="module", params=[3, 11], ids=["seed3", "seed11"])
def case(request):
    source = SyntheticBarSource(request.param, date(2014, 1, 1), date(2019, 1, 1))
    bars = source.fetch("GOLD", Timeframe.H1)
    m1 = source.fetch("GOLD", Timeframe.M1)
    combinations = expand_grid(validate(SPEC))
    wf = walk_forward(combinations, bars, COST, m1, years=YEARS)
    own = [simulate(bars, compile_signals(c, bars), COST, m1).records for c in combinations]
    stressed = [
        simulate(bars, compile_signals(c, bars), COST.stressed(), m1).records for c in combinations
    ]
    return bars, wf, own, stressed


def test_choices_match_an_independent_recomputation(case):
    bars, wf, own, _ = case

    for k, year in enumerate(YEARS):
        expected = choose(own, Window.years(year - 3, year - 1), bars.timeframe.seconds)
        assert wf.choices[k] == expected.chosen
        assert wf.train_trades[:, k].tolist() == expected.trades.tolist()
    holdout = choose(own, Window.years(YEARS[-1] - 2, YEARS[-1]), bars.timeframe.seconds)
    assert wf.holdout_choice == holdout.chosen
    assert set(wf.signals) == {c for c in (*wf.choices, wf.holdout_choice) if c is not None}


def test_the_stitched_record_never_holds_two_positions(case):
    _, wf, _, _ = case
    t = wf.trades.records

    assert len(t) > 50
    assert np.all(t["entry_idx"][1:] > t["exit_idx"][:-1])


def test_every_stitched_trade_is_its_own_combinations_trade(case):
    _, wf, own, _ = case

    for record, fold in zip(wf.trades.records, wf.trade_fold, strict=True):
        source = own[wf.choices[fold]]
        [row] = np.flatnonzero(source["entry_idx"] == record["entry_idx"])
        assert source[row].tobytes() == record.tobytes()  # every field, exits included
        year = datetime.fromtimestamp(int(record["entry_time"]), UTC).year
        assert year == YEARS[fold]


def test_fold_results_add_up(case):
    _, wf, own, _ = case

    assert wf.fold_net_r.sum() == pytest.approx(wf.metrics.net_r)
    assert wf.metrics.trades == len(wf.trades)
    for i, record in enumerate(own):
        for k, year in enumerate(YEARS):
            window = Window.years(year, year)
            entered = (record["entry_time"] >= window.start) & (record["entry_time"] < window.end)
            assert wf.blind_trades[i, k] == entered.sum()
            assert wf.blind_net_r[i, k] == pytest.approx(record["r"][entered].sum())


def test_daily_r_per_combination_matches_metrics_on_the_blind_bars(case):
    _, wf, own, _ = case
    blind = Window.years(YEARS[0], YEARS[-1])

    for i, record in enumerate(own):
        entered = record[(record["entry_time"] >= blind.start) & (record["entry_time"] < blind.end)]
        expected = metrics(Trades(entered), wf.blind_bars)
        assert wf.day_start.tolist() == expected.day_start.tolist()
        assert wf.daily_r[:, i] == pytest.approx(expected.daily_r)
        assert wf.blind_sharpe[i] == pytest.approx(sharpe(expected.daily_r))
        assert wf.blind_pf[i] == pytest.approx(expected.profit_factor)
    assert np.isfinite(wf.blind_sharpe).all()


def test_stress_keeps_the_choices_and_its_trades_come_from_the_stressed_simulation(case):
    _, wf, _, stressed = case

    for record, fold in zip(wf.stress_trades.records, wf.stress_trade_fold, strict=True):
        source = stressed[wf.choices[fold]]
        [row] = np.flatnonzero(source["entry_idx"] == record["entry_idx"])
        assert source[row].tobytes() == record.tobytes()
    t = wf.stress_trades.records
    assert np.all(t["entry_idx"][1:] > t["exit_idx"][:-1])


def test_stress_is_never_better_for_a_year_that_trades_the_same_way(case):
    # Doubling the spread can move an exit (a stop now inside the spread exits at the open), so
    # only years with the same trades, entry to exit, must be strictly no better. The total
    # over both years is lower in every case.
    _, wf, _, _ = case

    def key(records, folds, k):
        part = records[folds == k]
        return list(zip(part["entry_idx"], part["exit_idx"], part["exit_reason"], strict=True))

    for k in range(len(YEARS)):
        if key(wf.trades.records, wf.trade_fold, k) == key(
            wf.stress_trades.records, wf.stress_trade_fold, k
        ):
            assert wf.stress_fold_net_r[k] <= wf.fold_net_r[k] + 1e-9
    assert wf.stress_metrics.net_r < wf.metrics.net_r


def test_bars_past_the_last_blind_year_are_refused():
    bars = SyntheticBarSource(1, date(2016, 1, 1), date(2019, 2, 1)).fetch("GOLD", Timeframe.H1)

    with pytest.raises(ValueError, match="last blind year"):
        walk_forward(expand_grid(validate(SPEC)), bars, COST, years=YEARS)
