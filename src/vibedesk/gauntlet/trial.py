"""One Trial of a Hypothesis, start to finish, and the Holdout that may follow (spec §6).

`run_trial` checks the Trial Budget, fetches the Research Window through the market facade,
runs the Gauntlet (walk-forward, spread stress, the Coin-Flip Baseline, the Deflated Sharpe,
PBO and the Prosecutor), decides the Verdict and records the Trial in the Ledger. The record is
complete when it is written: every figure a Verdict Card shows is in its result JSON (spec §1).

`request_holdout` is the gate in front of the Ledger's Holdout Request: only a CANDIDATE judged
under today's trade rules (vibedesk.rules), and only when the Holdout is expected to hold enough
trades to judge. `run_holdout` spends an approved grant: it trades the Combination chosen on the
last research years over the Holdout.

The result JSON never holds NaN or Infinity, which browsers' JSON.parse rejects: NaN is null,
and an infinite value (a profit factor with no losing trade) is the string "inf" or "-inf".
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from vibedesk.blocks.compile import compile_signals
from vibedesk.blocks.spec import expand_grid, validate
from vibedesk.engine.costs import CostSnapshot
from vibedesk.engine.metrics import Metrics, metrics
from vibedesk.engine.simulate import ExitReason, Trades, simulate
from vibedesk.gauntlet.baseline import RUNS, Baseline, coin_flip_baseline
from vibedesk.gauntlet.prosecutor import prosecute
from vibedesk.gauntlet.stats import deflated_sharpe, pbo
from vibedesk.gauntlet.verdict import CANDIDATE, Verdict, decide, pass_bar
from vibedesk.gauntlet.walkforward import (
    BLIND_YEARS,
    MIN_TRAIN_TRADES,
    TRAIN_YEARS,
    WalkForward,
    walk_forward,
)
from vibedesk.ledger import BudgetExhausted, Ledger, TrialRecord
from vibedesk.market.bars import Timeframe
from vibedesk.market.data import MarketData
from vibedesk.market.grant import HOLDOUT_START, HoldoutGrant
from vibedesk.rules import RULES_VERSION

RESULT_VERSION = 2  # 2: records name their rules_version
MIN_HOLDOUT_TRADES = 30
MIN_HOLDOUT_PROFIT_FACTOR = 1.1
PASS, HOLDOUT_FAIL = "PASS", "HOLDOUT FAIL"
YEAR = 365.25 * 86400


class HoldoutRefused(Exception):
    """A Holdout Request the gate turned away; nothing was recorded or spent."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason  # NOT_CANDIDATE, RULES_CHANGED or INSUFFICIENT_TRADES


@dataclass(frozen=True, slots=True, eq=False)
class TrialResult:
    trial_id: int
    verdict: Verdict
    walk_forward: WalkForward
    baseline: Baseline
    record: dict  # exactly what the Ledger stored as the result JSON, parsed


@dataclass(frozen=True, slots=True, eq=False)
class HoldoutResult:
    spend_id: int
    verdict: str  # PASS or HOLDOUT FAIL
    trades: Trades
    metrics: Metrics
    record: dict


def run_trial(
    ledger: Ledger,
    market: MarketData,
    hypothesis_id: int,
    cost: CostSnapshot,
    *,
    use_m1: bool = True,
    runs: int = RUNS,
    years: tuple[int, ...] = BLIND_YEARS,
    train_years: int = TRAIN_YEARS,
    min_trades: int = MIN_TRAIN_TRADES,
) -> TrialResult:
    """Run one Trial of a Hypothesis and record it. Refused before any work once the family's
    Trial Budget is spent (BudgetExhausted)."""
    hypothesis = ledger.hypothesis(hypothesis_id)
    family_id = hypothesis.family_id
    used, budget = ledger.trial_count(family_id), ledger.budget(family_id)
    if used >= budget:
        raise BudgetExhausted(family_id, used, budget)
    if not years or years[-1] >= HOLDOUT_START.year:
        raise ValueError(f"blind years must end before the Holdout, got {years}")

    start = datetime(years[0] - train_years, 1, 1, tzinfo=UTC)
    end = datetime(years[-1] + 1, 1, 1, tzinfo=UTC)  # never past HOLDOUT_START
    timeframe = Timeframe(hypothesis.timeframe)
    bars = market.bars(hypothesis.symbol, timeframe, start, end)
    m1 = market.bars(hypothesis.symbol, Timeframe.M1, start, end) if use_m1 else None

    combinations = expand_grid(validate(hypothesis.spec_json))
    wf = walk_forward(
        combinations,
        bars,
        cost,
        m1,
        years=years,
        train_years=train_years,
        min_trades=min_trades,
    )
    trial_number = used + 1
    seed = _seed(family_id, hypothesis_id, trial_number)
    baseline = coin_flip_baseline(wf, seed=seed, runs=runs)
    n_trials, var_sharpe = ledger.deflation_inputs(family_id, pending_sharpes=wf.blind_sharpe)
    dsr = deflated_sharpe(wf.metrics.daily_r, n_trials, var_sharpe)
    overfit = pbo(wf.daily_r) if len(combinations) > 1 else math.nan
    verdict = decide([*pass_bar(wf, baseline, dsr, overfit), *prosecute(wf)])

    record = _plain(
        {
            "version": RESULT_VERSION,
            "rules_version": RULES_VERSION,
            "hypothesis_id": hypothesis_id,
            "family_id": family_id,
            "trial_number": trial_number,
            "symbol": hypothesis.symbol,
            "timeframe": hypothesis.timeframe,
            "spec": json.loads(hypothesis.spec_json),
            "verdict": verdict.verdict,
            "closest_miss": verdict.closest_miss,
            "checks": [c.to_dict() for c in verdict.checks],
            "cost": json.loads(cost.to_json()),
            "walk_forward": _walk_forward_record(wf),
            "baseline": {
                "seed": seed,
                "runs": runs,
                "p95": baseline.p95,
                "percentile": baseline.percentile,
                "strategy_net_r": baseline.strategy_net_r,
                "net_r": baseline.net_r,
                "fold_trades": baseline.fold_trades,
            },
            "deflated_sharpe": {"value": dsr, "n_trials": n_trials, "var_sharpe": var_sharpe},
            "pbo": overfit,
            "holdout": {"rate_per_year": _holdout_rate(wf)},
        }
    )
    trial_id = ledger.record_trial(
        hypothesis_id,
        combinations=list(zip(combinations, wf.blind_sharpe.tolist(), strict=True)),
        verdict=verdict.verdict,
        closest_miss=verdict.closest_miss,
        cost=cost,
        bars=bars,
        rules_version=RULES_VERSION,
        result_json=json.dumps(record, allow_nan=False),
    )
    return TrialResult(trial_id, verdict, wf, baseline, record)


def expected_holdout_trades(record: dict, now: datetime) -> float:
    """Trades the Holdout should hold: the Holdout Combination's blind trades per year, times
    the years from HOLDOUT_START to `now`."""
    years = max(0.0, (now - HOLDOUT_START).total_seconds() / YEAR)
    return record["holdout"]["rate_per_year"] * years


def request_holdout(ledger: Ledger, hypothesis_id: int, now: datetime | None = None) -> int:
    """Ask for the Holdout of a Hypothesis whose latest Trial is a CANDIDATE.

    Refused, with nothing recorded, unless the latest Trial is a CANDIDATE judged under today's
    trade rules and the Holdout is expected to hold at least MIN_HOLDOUT_TRADES trades. Then the
    Ledger records the request, which waits for the owner (and refuses once the family has spent
    its Holdout).
    """
    now = now or datetime.now(UTC)
    trial = _current_candidate(ledger, hypothesis_id)
    expected = expected_holdout_trades(json.loads(trial.result_json), now)
    if expected < MIN_HOLDOUT_TRADES:
        raise HoldoutRefused(
            "INSUFFICIENT_TRADES",
            f"about {expected:.0f} trades expected, at least {MIN_HOLDOUT_TRADES} needed",
        )
    return ledger.request_holdout(hypothesis_id)


def run_holdout(
    ledger: Ledger,
    market: MarketData,
    grant: HoldoutGrant,
    *,
    now: datetime | None = None,
    use_m1: bool = True,
    train_years: int = TRAIN_YEARS,
) -> HoldoutResult:
    """Spend the family's Holdout on the Hypothesis the grant was approved for.

    The latest Trial must still be a CANDIDATE under today's trade rules, as when the request
    was made. A Trial run since then (one more N to deflate by) can have turned the Verdict,
    and a rule change since then would judge the Holdout by other rules than its Trial. Either
    way the Holdout is refused before any bar is fetched.

    The Holdout trades the Combination that Trial chose on the last research years, under
    that Trial's Cost Model. It is simulated over all the data, so its indicators
    are warm, and judged on the trades entered from HOLDOUT_START on: PASS with a profit
    factor of at least MIN_HOLDOUT_PROFIT_FACTOR over at least MIN_HOLDOUT_TRADES trades.
    """
    now = now or datetime.now(UTC)
    hypothesis = ledger.hypothesis(ledger.holdout_request(grant.request_id).hypothesis_id)
    trial = _current_candidate(ledger, hypothesis.id)
    stored = json.loads(trial.result_json)
    chosen = stored["walk_forward"]["holdout_choice"]
    if chosen is None:
        raise HoldoutRefused("NOT_CANDIDATE", "the Trial chose no Combination for the Holdout")
    combination = expand_grid(validate(hypothesis.spec_json))[chosen]

    first_blind = stored["walk_forward"]["years"][0]
    start = datetime(first_blind - train_years, 1, 1, tzinfo=UTC)
    timeframe = Timeframe(hypothesis.timeframe)
    bars = market.bars(hypothesis.symbol, timeframe, start, now, grant=grant)
    m1 = market.bars(hypothesis.symbol, Timeframe.M1, start, now, grant=grant) if use_m1 else None
    records = simulate(bars, compile_signals(combination, bars), trial.cost, m1).records
    holdout = Trades(records[records["entry_time"] >= int(HOLDOUT_START.timestamp())])
    holdout_bars = bars.between(int(HOLDOUT_START.timestamp()), int(now.timestamp()) + 1)
    result = metrics(holdout, holdout_bars)

    passed = len(holdout) >= MIN_HOLDOUT_TRADES and result.profit_factor >= (
        MIN_HOLDOUT_PROFIT_FACTOR
    )
    verdict = PASS if passed else HOLDOUT_FAIL
    record = _plain(
        {
            "version": RESULT_VERSION,
            "rules_version": RULES_VERSION,
            "hypothesis_id": hypothesis.id,
            "trial_id": trial.id,
            "combination": combination.label(),
            "verdict": verdict,
            "trades": len(holdout),
            "profit_factor": result.profit_factor,
            "net_r": result.net_r,
            "thresholds": {
                "trades": MIN_HOLDOUT_TRADES,
                "profit_factor": MIN_HOLDOUT_PROFIT_FACTOR,
            },
            "ended_at_data": int(np.sum(holdout["exit_reason"] == ExitReason.END)),
            "daily_r": {"day_start": result.day_start, "r": result.daily_r},
            "trade_list": _trade_list(holdout),
        }
    )
    spend_id = ledger.record_holdout(grant, verdict, json.dumps(record, allow_nan=False))
    return HoldoutResult(spend_id, verdict, holdout, result, record)


def _current_candidate(ledger: Ledger, hypothesis_id: int) -> TrialRecord:
    """The latest Trial, which must be a CANDIDATE judged under today's trade rules."""
    trials = ledger.trials_for(hypothesis_id)
    if not trials or trials[-1].verdict != CANDIDATE:
        latest = trials[-1].verdict if trials else "no Trial"
        raise HoldoutRefused("NOT_CANDIDATE", f"the latest Trial is {latest}")
    trial = trials[-1]
    if trial.rules_version != RULES_VERSION:
        if trial.rules_version is None:
            ran = "before rules were versioned"
        else:
            ran = f"under rules version {trial.rules_version}"
        raise HoldoutRefused(
            "RULES_CHANGED",
            f"the latest Trial ran {ran}; today's rules are version {RULES_VERSION}."
            " Run a new Trial first",
        )
    return trial


# The stored record ---------------------------------------------------------------------------


def _walk_forward_record(wf: WalkForward) -> dict:
    return {
        "years": wf.years,
        "combinations": [c.label() for c in wf.combinations],
        "choices": wf.choices,
        "holdout_choice": wf.holdout_choice,
        "train_trades": wf.train_trades,
        "train_pf": wf.train_pf,
        "blind_trades": wf.blind_trades,
        "blind_net_r": wf.blind_net_r,
        "blind_sharpe": wf.blind_sharpe,
        "blind_pf": wf.blind_pf,
        "metrics": _metrics_record(wf.metrics),
        "fold_net_r": wf.fold_net_r,
        "stress_metrics": _metrics_record(wf.stress_metrics),
        "stress_fold_net_r": wf.stress_fold_net_r,
        "daily_r": {"day_start": wf.metrics.day_start, "r": wf.metrics.daily_r},
        "trade_list": _trade_list(wf.trades.records, wf.trade_fold),
    }


def _metrics_record(m: Metrics) -> dict:
    return {
        "trades": m.trades,
        "net_r": m.net_r,
        "profit_factor": m.profit_factor,
        "win_rate": m.win_rate,
        "expectancy_r": m.expectancy_r,
        "top5_share": m.top5_share,
        "max_drawdown_r": m.max_drawdown_r,
        "max_drawdown_money": m.max_drawdown_money,
        "max_drawdown_pct": m.max_drawdown_pct,
    }


def _trade_list(records: np.ndarray, folds: np.ndarray | None = None) -> dict:
    """The trades as columns, compact enough to store every one."""
    columns = {
        "entry_time": records["entry_time"],
        "exit_time": records["exit_time"],
        "side": records["side"],
        "entry_price": records["entry_price"],
        "exit_price": records["exit_price"],
        "r": records["r"],
        "exit_reason": [ExitReason(int(x)).name for x in records["exit_reason"]],
    }
    if folds is not None:
        columns["fold"] = folds
    return columns


def _holdout_rate(wf: WalkForward) -> float:
    """Blind trades per year of the Combination the Holdout would trade; 0 without one."""
    if wf.holdout_choice is None:
        return 0.0
    return float(wf.blind_trades[wf.holdout_choice].mean())


def _seed(family_id: int, hypothesis_id: int, trial_number: int) -> int:
    """The Coin-Flip Baseline's seed, fixed by which Trial this is, so a rerun draws the same."""
    return int(
        np.random.SeedSequence([family_id, hypothesis_id, trial_number]).generate_state(1)[0]
    )


def _plain(value):
    """JSON-safe: numpy to Python, NaN to None, infinities to "inf" / "-inf"."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, np.ndarray):
        return [_plain(v) for v in value.tolist()]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
    return value
