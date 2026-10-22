"""The Coin-Flip Baseline: what random entries earn with the strategy's timing and exits.

Spec §6.3 and tasks/plan.md. Every run draws, for every blind year, random trades with exactly
the strategy's trade count, the same server hour for each entry and the same number of longs
and shorts. They trade with that year's chosen Combination's exits under the same Cost Model.
The strategy must beat the 95th percentile of the runs' net R. Exact counts matter: fewer
random trades narrow the distribution and make it easier to beat.

Each random trade is drawn on its own, without the one-position rule: two random trades of a
run may overlap in time (owner's decision, 2026-10-22). Forcing a run's random trades to fit
one at a time between each other biases it whenever the strategy is in the market most of the
time: only short trades fit, and short trades are mostly stop-outs. At about 95% time in the
market a no-edge Spec beat 100% of such runs; drawn independently it lands at 0.48. Overlap
also widens the spread of the runs, which makes the 95th percentile harder to beat, never
easier. (Deferring a blocked entry to the next flat bar, the first plan, would have piled
random entries onto the hours just after exits.)

Entered from flat, a trade's path depends only on its entry bar and side: the stop, target,
trailing, time and opposite-signal exits are all functions of the bars. So every possible
random trade of a year is simulated once by the real engine, and a run picks from that table.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from vibedesk.engine.prepared import PreparedMarket
from vibedesk.engine.simulate import TRADE_DTYPE, Signals, simulate_prepared
from vibedesk.gauntlet.walkforward import WalkForward, Window
from vibedesk.market.server_time import server_offset

RUNS = 1000
HOUR = 3600


@dataclass(frozen=True, slots=True, eq=False)
class FoldPlan:
    """One blind year: what its random trades must match, and every trade they could be."""

    fold: int
    signals: Signals  # the year's chosen Combination; only its entries are replaced
    hours: np.ndarray  # server hour of each of the strategy's entries this year
    sides: np.ndarray  # +1 / -1 of each of the strategy's entries this year
    candidates: np.ndarray  # sorted entry bars of the needed hours, with a usable stop
    groups: np.ndarray  # for each entry, the index of its hour among the needed hours
    group_offsets: np.ndarray  # members of hour group g: group_members[offsets[g]:offsets[g+1]]
    group_members: np.ndarray  # candidate indexes, grouped by hour
    trades: np.ndarray  # 2 × candidates TRADE_DTYPE: row 0 long, row 1 short, entered from flat


@dataclass(frozen=True, slots=True, eq=False)
class Baseline:
    seed: int
    net_r: np.ndarray  # one per run
    p95: float
    strategy_net_r: float
    percentile: float  # share of runs the strategy beats, ties counted half
    fold_trades: np.ndarray  # the strategy's trades per fold
    run_fold_trades: np.ndarray  # runs × folds: the trades each run drew
    hour_counts: np.ndarray  # random entries per server hour, summed over every run
    long_trades: int  # random longs, summed over every run


def fold_plans(wf: WalkForward) -> list[FoldPlan]:
    """One plan per blind year in which the strategy traded."""
    market = wf.market
    hour = _server_hours(market.time)
    stitched = wf.trades.records
    plans = []
    for fold, chosen in enumerate(wf.choices):
        mine = stitched[wf.trade_fold == fold]
        if chosen is None or not len(mine):
            continue
        signals = wf.signals[chosen]
        window = Window.years(wf.years[fold], wf.years[fold])
        lo, hi = np.searchsorted(market.time, [window.start, window.end])
        entry_bars = np.arange(max(lo, 1), hi)
        # A random entry at bar t is a signal at bar t - 1, which needs a usable stop there.
        usable = entry_bars[signals.stop_dist[entry_bars - 1] > 0]
        hours = hour[mine["entry_idx"]]
        needed, groups = np.unique(hours, return_inverse=True)
        candidates = usable[np.isin(hour[usable], needed)]
        candidate_group = np.searchsorted(needed, hour[candidates])
        members = np.argsort(candidate_group, kind="stable")
        offsets = np.searchsorted(candidate_group[members], np.arange(len(needed) + 1))
        plans.append(
            FoldPlan(
                fold=fold,
                signals=signals,
                hours=hours,
                sides=mine["side"].astype(np.int8),
                candidates=candidates,
                groups=groups.astype(np.int64),
                group_offsets=offsets.astype(np.int64),
                group_members=members.astype(np.int64),
                trades=_every_trade(signals, candidates, market),
            )
        )
    return plans


def draw_year(plan: FoldPlan, rng: np.random.Generator) -> np.ndarray:
    """One year of random trades, in entry order: one per strategy entry, at a distinct bar of
    the same server hour, with the strategy's sides shuffled among them."""
    sides = rng.permutation(plan.sides)
    picks = np.empty(len(sides), dtype=np.int64)
    for g in range(len(plan.group_offsets) - 1):
        slots = np.flatnonzero(plan.groups == g)
        members = plan.group_members[plan.group_offsets[g] : plan.group_offsets[g + 1]]
        picks[slots] = rng.choice(members, size=len(slots), replace=False)
    records = plan.trades[(sides == -1).astype(np.int64), picks]
    return records[np.argsort(records["entry_idx"], kind="stable")]


def coin_flip_baseline(wf: WalkForward, seed: int, runs: int = RUNS) -> Baseline:
    rng = np.random.default_rng(seed)
    plans = fold_plans(wf)
    hour = _server_hours(wf.market.time)
    net_r = np.zeros(runs)
    run_fold_trades = np.zeros((runs, len(wf.years)), dtype=np.int64)
    hour_counts = np.zeros(24, dtype=np.int64)
    long_trades = 0
    for run in range(runs):
        for plan in plans:
            records = draw_year(plan, rng)
            net_r[run] += records["r"].sum()
            run_fold_trades[run, plan.fold] = len(records)
            hour_counts += np.bincount(hour[records["entry_idx"]], minlength=24)
            long_trades += int((records["side"] == 1).sum())

    strategy = wf.metrics.net_r
    return Baseline(
        seed=seed,
        net_r=net_r,
        p95=float(np.percentile(net_r, 95)),
        strategy_net_r=strategy,
        percentile=float(np.mean(net_r < strategy) + 0.5 * np.mean(net_r == strategy)),
        fold_trades=np.bincount(wf.trade_fold, minlength=len(wf.years)),
        run_fold_trades=run_fold_trades,
        hour_counts=hour_counts,
        long_trades=long_trades,
    )


def entry_signals(signals: Signals, bars: np.ndarray, sides: np.ndarray, n: int) -> Signals:
    """`signals` with its entries replaced: a long or short entry at each of `bars`."""
    long = np.zeros(n, dtype=np.bool_)
    short = np.zeros(n, dtype=np.bool_)
    long[bars[sides == 1] - 1] = True
    short[bars[sides == -1] - 1] = True
    return replace(signals, long_entry=long, short_entry=short)


def _every_trade(signals: Signals, candidates: np.ndarray, market: PreparedMarket) -> np.ndarray:
    """The trade each candidate bar gives, long and short, each entered from flat.

    The engine takes the first entry and ignores the ones that land while it is open, so each
    pass yields the trades of a non-overlapping subset; the rest go into the next pass.
    """
    table = np.zeros((2, len(candidates)), dtype=TRADE_DTYPE)
    for row, side in enumerate((1, -1)):
        remaining = candidates
        while len(remaining):
            sides = np.full(len(remaining), side, dtype=np.int8)
            records = simulate_prepared(
                market, entry_signals(signals, remaining, sides, len(market))
            ).records
            table[row, np.searchsorted(candidates, records["entry_idx"])] = records
            remaining = remaining[~np.isin(remaining, records["entry_idx"])]
    return table


def _server_hours(time: np.ndarray) -> np.ndarray:
    return ((time + server_offset(time)) // HOUR) % 24
