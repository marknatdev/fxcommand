"""Walk-forward: choose parameters on past years, then trade them blind on the next (spec §6.2).

Every grid Combination is simulated once over the whole Research Window, and the folds are cut
from those records, so indicator warm-up is paid once and every trade keeps the exits of the
Combination that opened it. The rules (tasks/plan.md, "Walk-forward"):
- Years are UTC calendar years, the same calendar as HOLDOUT_START.
- A training trade enters inside the window and its exit bar closes by the window's end, so a
  trade still open at the end of training never feeds the choice. Trades closed only because
  the data ended (ExitReason.END) never count either: that close is an artefact.
- A fold chooses the Combination with the best training profit factor among those with at
  least MIN_TRAIN_TRADES training trades; ties go to the earlier one in grid order. With none
  eligible the fold trades nothing, which makes it a losing fold.
- A fold's blind trades are its chosen Combination's trades entered in the blind year. A trade
  is dropped when it enters before the previous kept trade's exit bar is over, so the stitched
  record never holds two positions. A reversal, which enters at the open its predecessor left
  at, survives a fold boundary only when both trades come from the same Combination's
  simulation; from two different simulations the earlier one may have left later in that bar.
- Spread stress re-simulates the chosen Combinations at 2x spread and stitches them with the
  same choices; parameters are never re-chosen under stress.
- The Holdout's parameters are chosen the same way on the last TRAIN_YEARS research years.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from vibedesk.blocks.compile import BlockCache, compile_signals
from vibedesk.blocks.registry import LIBRARY, BlockRegistry
from vibedesk.blocks.spec import Combination
from vibedesk.engine.costs import CostSnapshot
from vibedesk.engine.metrics import Metrics, metrics, profit_factor
from vibedesk.engine.prepared import PreparedMarket, prepare
from vibedesk.engine.simulate import TRADE_DTYPE, ExitReason, Signals, Trades, simulate_prepared
from vibedesk.gauntlet.stats import sharpe
from vibedesk.market.bars import Bars
from vibedesk.market.server_time import DAY, server_days, server_offset

BLIND_YEARS = tuple(range(2017, 2025))
TRAIN_YEARS = 3
MIN_TRAIN_TRADES = 30


def year_start(year: int) -> int:
    """UTC epoch seconds of 1 January of `year`."""
    return int(datetime(year, 1, 1, tzinfo=UTC).timestamp())


@dataclass(frozen=True, slots=True)
class Window:
    """A half-open span [start, end) of UTC epoch seconds."""

    start: int
    end: int

    @classmethod
    def years(cls, first: int, last: int) -> Window:
        """1 January of `first` up to 1 January of `last` + 1."""
        return cls(year_start(first), year_start(last + 1))


@dataclass(frozen=True, slots=True, eq=False)
class Choice:
    chosen: int | None  # index into the Combinations; None when none was eligible
    trades: np.ndarray  # training trades per Combination
    profit_factor: np.ndarray  # training profit factor per Combination; NaN without trades


def training_mask(records: np.ndarray, window: Window, bar_seconds: int) -> np.ndarray:
    """The trades that may feed a choice made on `window`."""
    return (
        (records["entry_time"] >= window.start)
        & (records["exit_time"] + bar_seconds <= window.end)
        & (records["exit_reason"] != ExitReason.END)
    )


def choose(
    records: Sequence[np.ndarray],
    window: Window,
    bar_seconds: int,
    min_trades: int = MIN_TRAIN_TRADES,
) -> Choice:
    """The Combination with the best training profit factor among the eligible ones."""
    trades = np.zeros(len(records), dtype=np.int64)
    pf = np.full(len(records), np.nan)
    for i, record in enumerate(records):
        mask = training_mask(record, window, bar_seconds)
        trades[i] = mask.sum()
        if trades[i]:
            pf[i] = profit_factor(record["r"][mask])
    eligible = trades >= min_trades
    if not eligible.any():
        return Choice(None, trades, pf)
    # A profit factor of NaN with trades means every trade was exactly 0R: no edge at all.
    score = np.where(eligible, np.nan_to_num(pf, nan=0.0, posinf=np.inf), -np.inf)
    return Choice(int(np.argmax(score)), trades, pf)  # argmax keeps the first of equal scores


def stitch(
    records: Sequence[np.ndarray], choices: Sequence[int | None], windows: Sequence[Window]
) -> tuple[np.ndarray, np.ndarray]:
    """The blind record: each fold's chosen trades entered in its window, never overlapping.

    Returns the trade records and the fold index of each.
    """
    parts, folds = [], []
    last_exit, last_chosen = -1, None
    for fold, (chosen, window) in enumerate(zip(choices, windows, strict=True)):
        if chosen is None:
            continue
        record = records[chosen]
        after = record["entry_idx"] > last_exit
        if chosen == last_chosen:  # the same simulation: its own reversals never overlap
            after |= record["entry_idx"] == last_exit
        keep = (record["entry_time"] >= window.start) & (record["entry_time"] < window.end) & after
        part = record[keep]
        if len(part):
            parts.append(part)
            folds.append(np.full(len(part), fold, dtype=np.int64))
            last_exit, last_chosen = int(part["exit_idx"][-1]), chosen
    if not parts:
        return np.empty(0, dtype=TRADE_DTYPE), np.empty(0, dtype=np.int64)
    return np.concatenate(parts), np.concatenate(folds)


@dataclass(frozen=True, slots=True, eq=False)
class WalkForward:
    """Everything a Trial's checks and its Ledger record need from the walk-forward."""

    combinations: tuple[Combination, ...]
    years: tuple[int, ...]  # the blind year of each fold
    choices: tuple[int | None, ...]  # the chosen Combination of each fold
    holdout_choice: int | None  # the Combination the Holdout would trade

    # Combinations × folds.
    train_trades: np.ndarray
    train_pf: np.ndarray
    blind_trades: np.ndarray  # each Combination's own trades entered in the fold's year
    blind_net_r: np.ndarray

    # Each Combination's own trades over all blind years, for PBO, V[SR] and the Prosecutor.
    day_start: np.ndarray  # UTC epoch seconds of each blind server day
    daily_r: np.ndarray  # blind days × Combinations
    blind_sharpe: np.ndarray  # per Combination; 0.0 when it never traded
    blind_pf: np.ndarray  # per Combination; NaN when it never traded

    # The stitched blind record, at normal and at 2x spread.
    trades: Trades
    trade_fold: np.ndarray  # the fold of each stitched trade
    metrics: Metrics  # on the blind bars
    fold_net_r: np.ndarray
    stress_trades: Trades
    stress_trade_fold: np.ndarray
    stress_metrics: Metrics
    stress_fold_net_r: np.ndarray

    # What the Coin-Flip Baseline needs to re-simulate the folds' exits.
    market: PreparedMarket
    stressed: PreparedMarket
    signals: dict[int, Signals]  # compiled Signals of every chosen Combination
    blind_bars: Bars


def walk_forward(
    combinations: Sequence[Combination],
    bars: Bars,
    cost: CostSnapshot,
    m1: Bars | None = None,
    *,
    years: Sequence[int] = BLIND_YEARS,
    train_years: int = TRAIN_YEARS,
    min_trades: int = MIN_TRAIN_TRADES,
    registry: BlockRegistry = LIBRARY,
) -> WalkForward:
    """Walk-forward over `bars`, which must not reach past the last blind year."""
    years = tuple(years)
    if not years or list(years) != list(range(years[0], years[-1] + 1)):
        raise ValueError(f"blind years must be consecutive, got {years}")
    blind = Window.years(years[0], years[-1])
    if len(bars) == 0 or bars.time[-1] >= blind.end:
        raise ValueError("bars must end inside the last blind year")
    bar_seconds = bars.timeframe.seconds

    market = prepare(bars, cost, m1)
    stressed = prepare(bars, cost.stressed(), m1)
    cache = BlockCache(bars)
    signals = [compile_signals(c, bars, registry, cache) for c in combinations]
    records = [simulate_prepared(market, s).records for s in signals]

    windows = [Window.years(y, y) for y in years]
    fold_choices = [
        choose(records, Window.years(y - train_years, y - 1), bar_seconds, min_trades)
        for y in years
    ]
    choices = tuple(c.chosen for c in fold_choices)
    holdout = choose(
        records, Window.years(years[-1] - train_years + 1, years[-1]), bar_seconds, min_trades
    )
    chosen = sorted({c for c in (*choices, holdout.chosen) if c is not None})

    blind_trades = np.zeros((len(records), len(years)), dtype=np.int64)
    blind_net_r = np.zeros((len(records), len(years)))
    for i, record in enumerate(records):
        for k, window in enumerate(windows):
            entered = (record["entry_time"] >= window.start) & (record["entry_time"] < window.end)
            blind_trades[i, k] = entered.sum()
            blind_net_r[i, k] = record["r"][entered].sum()

    lo = int(np.searchsorted(bars.time, blind.start))
    blind_bars = bars.between(blind.start, blind.end)
    day_start, bar_day = _blind_days(bars, lo)
    daily_r = np.zeros((len(day_start), len(records)))
    blind_pf = np.full(len(records), np.nan)
    for i, record in enumerate(records):
        own = record[(record["entry_time"] >= blind.start) & (record["entry_time"] < blind.end)]
        daily_r[:, i] = _daily(own, bar_day, len(day_start))
        if len(own):
            blind_pf[i] = profit_factor(own["r"])
    blind_sharpe = np.array([_finite_sharpe(daily_r[:, i]) for i in range(len(records))])

    stitched, trade_fold = stitch(records, choices, windows)
    stress_records = [None] * len(records)
    for i in chosen:
        stress_records[i] = simulate_prepared(stressed, signals[i]).records
    stress_stitched, stress_fold = stitch(stress_records, choices, windows)

    return WalkForward(
        combinations=tuple(combinations),
        years=years,
        choices=choices,
        holdout_choice=holdout.chosen,
        train_trades=np.stack([c.trades for c in fold_choices], axis=1),
        train_pf=np.stack([c.profit_factor for c in fold_choices], axis=1),
        blind_trades=blind_trades,
        blind_net_r=blind_net_r,
        day_start=day_start,
        daily_r=daily_r,
        blind_sharpe=blind_sharpe,
        blind_pf=blind_pf,
        trades=Trades(stitched),
        trade_fold=trade_fold,
        metrics=metrics(Trades(stitched), blind_bars),
        fold_net_r=_per_fold(stitched, trade_fold, len(years)),
        stress_trades=Trades(stress_stitched),
        stress_trade_fold=stress_fold,
        stress_metrics=metrics(Trades(stress_stitched), blind_bars),
        stress_fold_net_r=_per_fold(stress_stitched, stress_fold, len(years)),
        market=market,
        stressed=stressed,
        signals={i: signals[i] for i in chosen},
        blind_bars=blind_bars,
    )


def _blind_days(bars: Bars, lo: int) -> tuple[np.ndarray, np.ndarray]:
    """The server days of the bars from index `lo` on, and the day index of every bar.

    Bars before `lo` get -1; no blind trade exits on them.
    """
    server_day = server_days(bars.time)
    days, first = np.unique(server_day[lo:], return_index=True)
    day_start = (days * DAY - server_offset(bars.time[lo + first])).astype(np.int64)
    bar_day = np.full(len(bars), -1, dtype=np.int64)
    bar_day[lo:] = np.searchsorted(days, server_day[lo:])
    return day_start, bar_day


def _daily(records: np.ndarray, bar_day: np.ndarray, days: int) -> np.ndarray:
    if not len(records):
        return np.zeros(days)
    return np.bincount(bar_day[records["exit_idx"]], weights=records["r"], minlength=days)


def _finite_sharpe(daily: np.ndarray) -> float:
    """The Ledger needs a finite Sharpe for every Combination; one that never traded has 0."""
    value = sharpe(daily)
    return value if np.isfinite(value) else 0.0


def _per_fold(records: np.ndarray, folds: np.ndarray, n: int) -> np.ndarray:
    return np.bincount(folds, weights=records["r"], minlength=n) if len(records) else np.zeros(n)
