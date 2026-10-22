# Implementation Plan: Layer A, Milestone M2 (Gauntlet and Ledger)

## Overview

M2 turns the M1 engine into a Verdict. The `gauntlet` module runs one Trial of a Hypothesis through every Pass Bar check (spec §6.2–6.4). The `ledger` module records every Hypothesis and Trial, append-only, with Trial counting, budgets and Holdout Requests. The `market` facade becomes the only door to bars, and it locks the Holdout (spec §4).

M2 runs on `SyntheticBarSource` only. It uses no MT5 (that is M3) and no LLM (M4).

**Done when (spec §10):**
- the known-answer tests (§9.6) pass;
- a Trial on 12 years of synthetic M15 takes ≤ 60 s;
- the Holdout and Ledger guard tests pass.

Detailed tasks: [todo.md](todo.md). The finished M1 plan: [done/m1-plan.md](done/m1-plan.md).

## Definitions the tests depend on

These are fixed before code, so no test is tuned after the fact.

### Walk-forward (spec §6.2)
- **Folds:** blind years 2017 … 2024. Each fold trains on the 3 calendar years before its blind year.
- **One simulation per combination.** Every grid combination is compiled and simulated once over the whole Research Window (2014-01-01 → 2024-12-31). Folds are cut from that one record, never compiled per fold, so indicator warm-up is paid once.
- **Training metrics:** a trade counts for a training window only if it enters inside the window and exits before the window ends. A trade entered on 30 December and exiting in the blind year never feeds parameter selection.
- **Parameter selection:** the combination with the best training profit factor among those with ≥ 30 training trades. Ties go to the earlier combination in grid order. If no combination qualifies, the fold trades nothing and counts as a losing fold.
- **Blind trades:** each fold takes its trades from its chosen combination's own simulation: the trades entered inside the blind year. Each trade keeps that combination's exits until it closes, even after the year ends.
- **Boundaries:** at each fold boundary, drop next-fold trades that enter before the previous fold's last trade has exited. The stitched record therefore never holds two positions.
- **Spread stress:** the same fold selections are re-simulated at 2× spread. Parameter selection is not re-run under stress.
- **Holdout selection:** the choice uses 2022–2024 with the same rule (trades exiting by 2024-12-31). The trades are those entered on or after 2025-01-01, from a simulation over all data, which needs an approved Holdout grant.

### Coin-Flip Baseline (spec §6.3)
- **1,000 runs**, seeded from the Trial id.
- **Each run, each blind year:**
  - the same number of trades as the strategy took in that year;
  - entry bars drawn with the same server-hour distribution as the strategy's entries;
  - the same long/short mix;
  - the fold's chosen combination's exits (stop, target, trailing, time, opposite);
  - the same Cost Model.
- **Exact trade counts (amended 2026-10-22, owner's decision).** Each random trade is drawn on its own at a distinct bar of its server hour, from trades the engine simulated alone from flat. A run is not held to one position at a time. Deferring blocked entries piled them onto post-exit hours, and redrawing them one position at a time biased long-holding Specs toward short, losing trades (see T7 in todo.md).
- **Why exact counts matter:** fewer baseline trades would narrow the net-R distribution and make the 95th percentile easier to beat, which is a bias toward false CANDIDATEs. A test asserts that the realised counts equal the strategy's.

### Statistics
- **Deflated Sharpe (Bailey & López de Prado):**
  - Computed on the daily R of the stitched blind years: every trading day, including zero days. A trade's R is booked on its exit day.
  - It uses the non-annualised Sharpe and non-excess kurtosis.
  - N = all grid combinations evaluated across the Idea Family's Trials. V[SR] = the variance of those combinations' blind-year Sharpe.
  - N = 1 reduces it to the plain probabilistic Sharpe against 0.
- **PBO:**
  - CSCV over 16 equal time blocks of the blind years, using the grid combinations' daily-R Sharpe per block.
  - It covers all C(16, 8) = 12,870 splits.
  - A block with zero trades has a Sharpe of 0.
  - A grid of 1 combination is "n/a", which neither passes nor fails.

### Known answers (spec §9.6)
- **Planted edge:** a planted edge in *entry timing*, not a trend that any trailing exit would harvest. On each server day, a random hidden direction shows itself in the London opening hour and then drifts on for the next hours.
- **Known-answer Spec:** `range_breakout(london_open)` with a fixed `atr_stop` and an `r_target`. A random entry at the same hours has a 50/50 direction, so it gets no edge.
- **Thresholds, fixed now:**
  - Random walk: at most 1 CANDIDATE in 20 seeds.
  - Planted edge, at the strength calibrated in T2: CANDIDATE in at least 4 of 5 seeds.
  - Both use the real Pass Bar.

### Trial benchmark
- **Named configuration:**
  - 12 years of synthetic GOLD M15 with M1 settling;
  - `donchian_breakout(period ∈ {20, 40, 60, 80})` × `atr_stop(mult ∈ {1.5, 2, 2.5})` × `r_target(r ∈ {1.5, 2, 3, 4})` = 48 combinations, plus a `session_window` filter;
  - a full Gauntlet with 1,000 baseline runs.
- **Pass:** ≤ 60 s on this laptop.
- **Worst case:** a 48-combination grid at the maximum indicator bounds is measured and recorded, not asserted.

### Test policy
- Known-answer and benchmark tests are marked `slow`. They are excluded from the default `uv run pytest` and run at every checkpoint with `uv run pytest -m slow`.

## Architecture decisions

- **New modules:**
  - `src/vibedesk/gauntlet/`: `stats.py`, `walkforward.py`, `baseline.py`, `prosecutor.py`, `verdict.py`, `trial.py`.
  - `src/vibedesk/ledger/`: `store.py`.
  - `src/vibedesk/market/data.py`: the facade and the Holdout guard.
- **Prepared market:** `engine.prepare(bars, cost, m1) → PreparedMarket` hoists the per-call setup: spread prices for bars and minutes, and M1 index bounds. Repeated simulations (the grid, stress, 1,000 baseline runs) pay it once. `simulate(bars, signals, cost, m1)` stays as a thin wrapper, so M1's tests and the reference twin are untouched.
- **Block-output cache:** within a Trial, every (block, params) pair is computed once, however many combinations share it.
- **Holdout grant:** the contract (`HoldoutGrant`, the `GrantVerifier` Protocol and `HOLDOUT_START`) is fixed in `src/vibedesk/market/grant.py`, which is committed before Phase 1 and owned by no task. `MarketData` is given a `GrantVerifier` (implemented by the Ledger) and asks it whether a grant is approved, unspent and for this Idea Family. The grant's object type alone proves nothing. Spending is enforced by a UNIQUE constraint per Idea Family. In M2 the Ledger function is the gate; the API enforces owner-only approval in M5.
- **Ledger:** stdlib `sqlite3` with one writer (ADR 0006). Hypotheses, Trials and Holdout spends are append-only, enforced by triggers that abort UPDATE and DELETE, plus a test that scans the code.
- **The Trial record is complete at write time** (spec §1: no figure may differ from stored data). It holds:
  - the fold selections;
  - every combination's per-fold results;
  - every combination's blind-year Sharpe, which later Trials need for V[SR];
  - the baseline seed, distribution percentiles and realised counts;
  - the CostSnapshot;
  - every check's value, threshold and result, and the Closest Miss;
  - a fingerprint of the Research-Window bars (count, first and last time, checksum).

## Trial-cost budget (from Checkpoint C)

| Piece | Naive | Planned |
|---|---|---|
| Simulate the full window, with M1 | 91 ms | ~25 ms (setup hoisted) |
| 48 combinations: compile and simulate | ~11 s | ~3–5 s (Block cache) |
| Spread stress (8 folds re-simulated) | — | < 1 s |
| Coin-Flip Baseline, 1,000 × 8 blind years | ~60 s | ~15–20 s |
| PBO, 12,870 splits × 48 combinations | — | < 2 s, vectorised |
| **Trial** | **> 70 s** | **~25 s** |

If T10 measures more than 60 s, the levers are, in order:
1. run the baseline across cores;
2. size `_run`'s output by the number of signals instead of the number of bars;
3. make SMA and std O(n);
4. fewer baseline runs (needs the owner's OK, since it changes the spec);
5. a looser budget (also needs the owner's OK).

## Task list

### Phase 1: Foundations. T1–T5 own separate files and can run in parallel.
- [x] T1: Prepared market + Block-output cache (engine speed)
- [x] T2: Synthetic planted edge + calibration (**high risk; first**)
- [x] T3: Statistics: Sharpe, Deflated Sharpe, PBO
- [x] T4: Market facade + Holdout guard
- [x] T5: Ledger: families, Hypotheses, Trials, budgets, Holdout Requests

### Checkpoint A
- [x] Default suite green, ruff clean; T2's calibration numbers recorded; the owner reviews

### Phase 2: Gauntlet core (needs T1 and T3)
- [x] T6: Walk-forward: folds, selection, stitched blind record, stress, Holdout selection
- [x] T7: Coin-Flip Baseline with exact counts
- [x] T8: Prosecutor, Pass Bar, Verdict, Closest Miss

### Checkpoint B
- [x] Gauntlet pieces are green on hand-built inputs; the owner reviews

### Phase 3: Integration (needs everything)
- [x] T9: Trial runner + Holdout run + Holdout Request gate
- [x] T10: Known-answer tests + Trial benchmark (`slow`)

### Checkpoint C: M2 done
- [x] The spec §10 M2 items are met; `uv run pytest` and `uv run pytest -m slow` are both green

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| The planted edge can't pass the real Pass Bar, or random walk passes too often | High: M2 can't finish | T2 calibrates first, against a gross-PF target on the known-answer Spec; thresholds fixed above before tuning |
| Trial > 60 s | High | Hoisting + caching in T1; the levers above; T10 measures |
| Deflated Sharpe / PBO formula errors pass silently | High | Limit-case tests and a test-side formula written separately (T3); no recalled example numbers |
| Baseline too lenient (count drift, hour mismatch) | High | Exact counts by deferral, plus count and hour-distribution tests (T7) |
| Walk-forward leaks blind prices into selection | High | The exit-before-boundary rule, with a test whose leaking trade would flip the selection (T6) |
| Holdout opened by a path that bypasses `market` | High | A guard test: only `market` calls `BarSource.fetch`; grants checked against the Ledger (T4/T5) |
| Ledger rows edited after the fact | Medium | Triggers abort UPDATE/DELETE; a code scan for UPDATE/DELETE outside migrations |

## Open questions and follow-ups

- **Time filters:** should `session_window`/`day_of_week` judge the signal bar (as now) or the entry bar? It matters on H4/D1. Still open.
- **Stop and reverse (decided 2026-10-22, after M2):** an opposite signal on a Spec that allows the other side closes the trade and opens the reverse at the same open. This is an engine and reference-twin change, done as its own task after M2.
- **Known-answer floor (decided 2026-10-22):** T10 runs the known-answer test with a 17-point Spread Floor.
