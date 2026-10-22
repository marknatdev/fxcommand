# M2 tasks: Gauntlet and Ledger

- **Spec:** [docs/spec/layer-a-verdict-engine.md](../docs/spec/layer-a-verdict-engine.md), §4, §6, §9 and §10.
- **Plan and definitions:** [plan.md](plan.md). Tasks follow its "Definitions the tests depend on" exactly.
- **Commands:** `uv run pytest` (default suite), `uv run pytest -m slow` (known answers and benchmark), `uv run ruff check`, `uv run ruff format --check`.

**File ownership.** Each Phase 1 task owns its files and touches no one else's, so T1–T5 can run in parallel worktrees. Shared files (`pyproject.toml`, `__init__.py` re-exports) change only in the task named for them.

---

## T1: Prepared market + Block-output cache

**Description:** Hoist the per-call setup out of `simulate`. The new `prepare(bars, cost, m1=None) -> PreparedMarket` holds the bar arrays, the spread prices for bars and minutes, and the M1 index bounds. Add `simulate_prepared(market, signals)`. `simulate(bars, signals, cost, m1)` stays as a wrapper. `prepare(...)` with a stressed `CostSnapshot` gives the 2× spread market. Size `_run`'s output table by the number of entry signals plus one, not by the number of bars. Add a `BlockCache` to `compile_signals`, so a (block, params) pair is computed once per cache.

**Acceptance criteria:**
- [x] On the 200 reference-twin seeds, `simulate_prepared` gives exactly the same records as `simulate`. The existing engine tests pass unchanged.
- [x] On 12 years of synthetic GOLD M15 with M1, a prepared simulation of the `tests/test_perf.py` Spec takes ≤ 30 ms (best of 5).
- [x] Compiling all 48 combinations of a grid that shares one `atr_regime(lookback=2000)` filter calls that filter exactly once (a counting test).

**Verification:** `uv run pytest tests/test_prepared.py tests/test_engine_props.py tests/test_engine_golden.py`

**Dependencies:** None

**Files (owned):** `src/vibedesk/engine/simulate.py`, `src/vibedesk/engine/prepared.py`, `src/vibedesk/blocks/compile.py`, `tests/test_prepared.py`

**Scope:** M

**Result (merged 2026-10-22, `e8e8b3b`):**
- **Records:** all 403 saved hashes (200 seeds at 1× and 2× spread, plus three 12-year runs) match the old engine exactly.
- **Speed:** a prepared simulation with M1 takes 4.0 ms (153 ms before), and `prepare` itself about 155 ms once per market. The 48-combination grid takes 0.53 s (9.6 s before).
- **Cache:** the shared `atr_regime(lookback=2000)` filter is computed once.
- **Deviations:**
  - Compiled signal arrays are read-only, because the cache shares them. T7 must build new arrays with `dataclasses.replace`.
  - A target is cached together with its stop.
  - A `BlockCache` is tied to one set of bars, so the Research Window and the Holdout need separate caches.
  - `PreparedMarket` also holds per-bar swap counts.

---

## T2: Synthetic planted edge + calibration (high risk; first)

**Description:** Replace the uncalibrated `trend` knob with a planted edge in entry timing (plan.md, "Known answers"). On each server day, draw a hidden direction. During the London opening hour, price drifts that way by `reveal` × hourly volatility. For the following `follow_hours` hours, it keeps drifting by `follow` × hourly volatility. Everything else stays a random walk. Calibrate `reveal` and `follow` empirically and write the numbers into this file.

**Acceptance criteria:**
- [x] With the edge off, the known-answer Spec (`range_breakout(london_open, 4)`, `atr_stop(mult=1.5)`, `r_target(r=2)`, GOLD M15, Spread Floor 55) has a profit factor after costs below 1.05 on each of 5 seeds over 2014–2024.
- [x] With the calibrated edge, the same Spec has a profit factor of at least 1.3 on each of 5 seeds over 2014–2024 and over 2025–2026. Random 50/50 entries at the same London hours with the same exits have a profit factor below 1.05.
- [x] The edge never changes bar structure: the existing synthetic tests (aggregation, weekends, the point grid) still pass. With the edge off, the bars are the same as before.

**Verification:** `uv run pytest tests/test_synthetic.py`, plus a calibration script whose output is pasted here.

**Dependencies:** None

**Files (owned):** `src/vibedesk/market/synthetic.py`, `tests/test_synthetic.py`, `tests/test_planted_edge.py`

**Scope:** M

**Result (merged 2026-10-22, `da81f96`):**
- **Parameters:** `PLANTED_EDGE = SyntheticConfig(reveal=2.0, follow=0.6, follow_hours=3)`, calibrated on seeds 1–5. The full write-up is [docs/research/2026-10-22-planted-edge.md](../docs/research/2026-10-22-planted-edge.md).
- **Accepted by the owner:** the hidden daily directions are balanced within 20-server-day blocks (10 up, 10 down, random order) instead of independent coin flips. Independent flips made the price level wander (seed 3: 1014 → 412), and the fixed-point Spread Floor then swamped the edge. Each day is still a fair coin, and lag-1 autocorrelation of daily returns moves from −0.004 to −0.031.
- **Profit factor after costs, seeds 1–5:**

  | | 2014–2024 | 2025–2026 |
  |---|---|---|
  | Edge off | 0.65–0.77 | 0.66–0.88 |
  | Edge on | 1.47–1.83 | 1.57–2.16 |
  | Random 50/50 entries, mean (max) | 0.77–0.90 (≤ 0.93) | 0.76–0.97 (≤ 1.12) |
  | Edge on, 2× spread | 0.86–1.36 | 0.91–1.70 |

- **Every year is profitable** with the edge on: per-year PF 1.11–2.46.
- **Edge off reproduces the bars from before the edge**, pinned by sha256.
- **Risk for T10:** 2× spread stress over 2017–2024 passes on only 4 of 5 seeds; seed 4 gives 0.80. On 20 fresh seeds it passes on 12. The cause is the 55-point floor on synthetic prices of about 400–1,150, which is much dearer relative to price than real GOLD. Undecided: a floor of about 17 points in the known-answer test only, matching real GOLD's cost relative to price, passes all 5 seeds and 15 of 20 fresh ones. This is the owner's call at T10.
- **PBO note for T10:** use a known-answer grid of 1, or one whose combinations really differ. Equal combinations push PBO toward 0.5.

---

## T3: Statistics: Sharpe, Deflated Sharpe, PBO

**Description:** Pure functions in `gauntlet/stats.py`:
- `sharpe(daily_r)`: not annualised.
- `probabilistic_sharpe(sr, benchmark, t, skew, kurtosis)`.
- `deflated_sharpe(daily_r, n_trials, var_trial_sharpe)`, following Bailey & López de Prado, with non-excess kurtosis and the Euler–Mascheroni constant.
- `pbo(matrix)`: CSCV over 16 blocks, all 12,870 splits, vectorised.

Use `statistics.NormalDist`, with no SciPy dependency.

**Acceptance criteria:**
- [x] The Deflated Sharpe limit cases hold:
  - N = 1 gives the probabilistic Sharpe against 0;
  - V[SR] = 0 gives a benchmark of 0;
  - it falls as N or V[SR] rises;
  - negative skew and fat tails lower it;
  - it matches a formula written separately in the test, on 50 random inputs.
- [x] The PBO cases hold:
  - a combination that is best in every block gives 0;
  - IID noise gives PBO 0.5 ± 0.1 as a mean over seeds 0–49 (amended 2026-10-22: a single noise matrix is too noisy for that band; over 200 seeds the sd is 0.179 and only 33% fall inside ±0.1);
  - a best-in-sample that is worst out-of-sample gives ≥ 0.9;
  - all-zero blocks are handled;
  - a grid of 1 is reported as "n/a".
- [x] PBO on a 48-combination × 16-block matrix takes ≤ 2 s.

**Verification:** `uv run pytest tests/test_stats.py`

**Dependencies:** None

**Files (owned):** `src/vibedesk/gauntlet/__init__.py`, `src/vibedesk/gauntlet/stats.py`, `tests/test_stats.py`

**Scope:** S

**Result (merged 2026-10-22, `6d1c97f`):**
- **API:** `sharpe`, `probabilistic_sharpe`, `expected_max_sharpe`, `deflated_sharpe` and `pbo(returns, blocks=16)`, where `returns` is days × combinations.
- **Conventions:**
  - A flat series has Sharpe 0.0; fewer than 2 days gives NaN.
  - Skew and kurtosis are population moments, with non-excess kurtosis.
  - PBO uses average out-of-sample ranks, with ω = rank/(N+1) and overfit when logit(ω) ≤ 0. Identical or all-zero columns give 1.0, and one column gives NaN.
- **Speed:** PBO on 2,000 × 48 takes 0.06 s.
- **Checking:** no paper numbers are pinned (the paper couldn't be fetched). The tests check the limit cases and an independently written formula.

---

## T4: Market facade + Holdout guard

**Description:** `MarketData(source: BarSource, verifier: GrantVerifier)`, with `bars(symbol, tf, start, end, grant=None)`. It refuses any range reaching 2025-01-01 (the `HOLDOUT_START` constant) unless `verifier.verify(grant, symbol)` approves. `HoldoutGrant` is a frozen dataclass: request id, Idea Family id, token. `GrantVerifier` is a Protocol; the Ledger implements it in T5. A fake verifier serves the tests.

**Acceptance criteria:**
- [x] Without a grant, any range ending after 2025-01-01 is refused with a clear error. Ranges ending on or before it work.
- [x] A grant the verifier rejects (forged, wrong family, already spent) is refused. An approved grant opens the Holdout.
- [x] A guard test fails if any module outside `vibedesk.market` calls `.fetch(` on a source.

**Verification:** `uv run pytest tests/test_holdout_guard.py`

**Dependencies:** None

**Files (owned):** `src/vibedesk/market/data.py`, `tests/test_holdout_guard.py`

**Scope:** S

**Result (merged 2026-10-22, `ac7f4bf`):**
- **API:** `MarketData(source, verifier).bars(symbol, timeframe, start, end, grant=None)`, raising `HoldoutLocked`. `verify` is called on every Holdout-reaching request and never cached.
- **Accepted by the owner:** without a grant, bars that open before 2025-01-01 but close after it are dropped. That is the H4 and D1 bar opening at 2024-12-31 22:00 UTC, which carries up to 22 hours of Holdout prices. Research data therefore ends with the last bar that closes by 2025-01-01.

---

## T5: Ledger

**Description:** `Ledger(path)` on stdlib `sqlite3`: the schema, migrations and append-only triggers.
- `create_family(idea)`
- `register_hypothesis(family_id, spec)`: freezes the Spec JSON, applies the anti-reset rule (a Spec with the same entry Block and symbol as another family's must use that family), and accepts one symbol and one timeframe per Hypothesis.
- `record_trial(...)`: stores the complete Trial record (plan.md list).
- `trial_count(family)`, `budget(family)` (25 + owner grants), `extend_budget(family, 25)`.
- `deflation_inputs(family) -> (n_combinations, var_sharpe)`.
- `request_holdout(hypothesis)`, `approve_holdout(request) -> HoldoutGrant`.
- `verify(grant, symbol)` and `record_holdout(grant, result)`: the spend is UNIQUE per family.

**Acceptance criteria:**
- [x] UPDATE and DELETE on hypotheses, trials and holdout spends abort, enforced by triggers. A code-scan test finds no UPDATE or DELETE statements outside the migrations.
- [x] The Trial Budget is enforced. A 26th Trial is refused until the owner grants +25, and the grant is itself a ledger row. `deflation_inputs` sums combinations across all of the family's Trials.
- [x] A Holdout grant verifies once for its own family and symbol, is refused after its spend is recorded, and is refused for any other family. A second spend in the same family violates the UNIQUE constraint. The anti-reset rule refuses a new family with the same entry Block and symbol.

**Verification:** `uv run pytest tests/test_ledger.py`

**Dependencies:** None (it implements T4's `GrantVerifier` Protocol by duck typing; the shapes are given here)

**Files (owned):** `src/vibedesk/ledger/__init__.py`, `src/vibedesk/ledger/store.py`, `tests/test_ledger.py`

**Scope:** M

**Result (merged 2026-10-22, `925a71f`, `0e7636a`):**
- **Schema:** 8 tables (families, hypotheses, trials, trial_combinations, budget_grants, holdout_requests, holdout_approvals, holdout_spends). Each has triggers that abort UPDATE and DELETE, and `recursive_triggers` makes `INSERT OR REPLACE` abort too.
- **Accepted by the owner:** V[SR] is the sample variance (n − 1), and 0.0 with fewer than 2 combinations.
- **For T9:**
  - `deflation_inputs(family, pending_sharpes=())` lets a Trial count itself before it is recorded.
  - `record_trial` refuses a partial grid, a non-finite Sharpe (send 0.0, not NaN), and bars for the wrong symbol or timeframe.
  - Verdicts are the plain strings `CANDIDATE`, `REJECT`, `PASS` and `HOLDOUT FAIL`.
  - `verify` never spends; only `record_holdout` does, and a second spend raises `HoldoutSpent`.

---

## Checkpoint A (after T1–T5)
- [x] `uv run pytest` and ruff are green: 711 passed and 1 skipped (the slow calibration test, which passes under `-m slow` in 57 s), on master after the merges (2026-10-22). The default suite takes 26.8 s, close to T10's 30 s limit.
- [x] T2's calibration numbers are recorded above
- [x] The owner reviewed and approved the merge in the order T4, T5, T3, T1, T2, accepting T2's balanced blocks, T5's sample variance and T4's bar trimming (2026-10-22)
- **Hand-over notes** from the Phase 1 sessions (contracts for T6–T10) are in `C:\Users\Administrator\AppData\Roaming\Claude\side-session-notes\local_78cf67f8-4836-42cc-b9e1-787d52d5b893\`: `t1-engine-speed.md`, `t2-planted-edge.md`, `t3-stats.md`, `t4-holdout-guard.md` and `t5-ledger-contract.md`.

---

## T6: Walk-forward

**Description:** Implement the plan.md walk-forward definition, in four steps:
1. Every combination of a Spec is compiled and simulated once over the Research Window, using T1's prepared market and Block cache.
2. Folds 2017–2024 each choose parameters on their 3 training years, using only trades that exit inside the window.
3. The stitched blind record follows the boundary rule.
4. The stress re-simulation and the Holdout parameter selection (2022–2024) reuse those choices.

The output is a `WalkForward` result holding:
- the per-fold choices;
- the per-combination per-fold metrics;
- the stitched blind trades;
- the per-combination blind daily R;
- the stress record.

**Acceptance criteria:**
- [x] On a hand-built grid of 3 combinations whose best combination changes by year, the choices per fold match the hand-worked answer. A trade that straddles the training boundary would flip one choice if it leaked, and the test shows it doesn't.
- [x] The stitched record never holds two positions at once, and every trade keeps its own combination's exit rules (checked by recomputing each trade from that combination's own simulation).
- [x] Stress uses the same fold choices, and each blind year's net R under stress is no better than without it.

**Verification:** `uv run pytest tests/test_walkforward.py`

**Dependencies:** T1

**Files (owned):** `src/vibedesk/gauntlet/walkforward.py`, `tests/test_walkforward.py`

**Scope:** M

**Result (2026-10-22, built here):**
- **API:** `walk_forward(combinations, bars, cost, m1=None, *, years=BLIND_YEARS, train_years=3, min_trades=30) -> WalkForward`, with the pure layer `choose(records, window, bar_seconds)` and `stitch(records, choices, windows)`. `WalkForward` holds:
  - the choices per fold and `holdout_choice`;
  - per Combination per fold: training trades, training PF, blind trades and blind net R;
  - per Combination: the blind daily-R matrix (days × Combinations) on one day grid, the blind Sharpe (0.0 when it never traded, so the Ledger gets a finite value) and the blind PF;
  - the stitched record and the stress record, each with its fold index and `Metrics`, plus net R per fold;
  - both prepared markets and the chosen Combinations' Signals, for T7.
- **Boundary rules, pinned in the module docstring:**
  - Years are UTC calendar years.
  - A training trade must also close its exit bar by the window's end. This matters for H4/D1 bars that open at 22:00 UTC on 31 December.
  - Trades closed only because the data ended (`ExitReason.END`) never count for training.
- **Tests:** a hand-worked 3-Combination grid with a straddling +20R trade and an END-closed +80R trade. Mutation check: counting END closes flips fold 2019, and judging training trades by entry alone flips fold 2018. On synthetic H1 with M1 (2 seeds), every stitched and stress trade is byte-identical to its Combination's own simulation.
- **Deviations:**
  - The stress criterion is restated, as at M1's T4. A doubled spread can change a trade's path, so only a year whose trades are the same, entry to exit, must be no better under stress, and total R must be lower.
  - `profit_factor(r)` is now a public function in `engine/metrics.py`.
  - The `slow` marker and `addopts = "-m 'not slow'"` are registered in `pyproject.toml` now, rather than in T10.
- **Default suite:** 736 passed in 21 s.

---

## T7: Coin-Flip Baseline

**Description:** `coin_flip_baseline(walk_forward, market, runs=1000, seed)` returns the net-R distribution and its 95th percentile. Random entries follow the plan.md definition: per-year counts, the server-hour distribution, the long/short mix, each fold's exits, and deferral to a flat bar, with up to 20 redraws if entries spill past the year.

**Acceptance criteria:**
- [x] The realised trade count per blind year equals the strategy's in every run of a 200-run test. Redraws are counted and reported.
- [x] The entry-hour histogram over 1,000 runs matches the strategy's within ± 2 percentage points per hour. The long share matches within ± 2 points.
- [x] Same seed, same distribution. On random-walk data, a random strategy's percentile against its own baseline is roughly uniform over 30 seeds (no bias).

**Verification:** `uv run pytest tests/test_baseline.py`

**Dependencies:** T1, T6

**Files (owned):** `src/vibedesk/gauntlet/baseline.py`, `tests/test_baseline.py`

**Scope:** M

**Result (2026-10-22, built here):**
- **API:** `coin_flip_baseline(wf, seed, runs=1000) -> Baseline`. The result holds the net-R distribution, p95, the strategy's percentile, the per-run and per-fold counts, the hour counts and the long count. Lower-level: `fold_plans(wf)` and `draw_year(plan, rng)`.
- **How the rule got here:**
  - The plan said to defer a blocked entry to the next flat bar. That piles entries onto the hours just after exits.
  - The owner then chose to redraw blocked entries at flat bars instead. That is unbiased at moderate occupancy, but biased at high occupancy: about 94% in the market made random trades 0.09–0.15R worse, and a no-edge Spec beat 100% of its runs. It could also fail outright when the count didn't fit.
  - **The owner's final decision (2026-10-22): independent draws.** Each run draws the strategy's exact count of random trades per year, at distinct bars of the same server hours and with the strategy's sides shuffled. The trades come from the real engine, each simulated alone from flat, and a run is not held to one position at a time.
- **Properties of the final rule:**
  - Hour and side mixes are exact.
  - No length bias at any occupancy.
  - It can't fail.
  - The spread is wider, so the 95th percentile is harder to beat.
- **Speed:**
  - Every candidate trade of a year is simulated once, about 10k candidates × 2 sides.
  - 1,000 runs on 11 years of M15 take about 4 s.
  - The engine skips bars before the first signal and stops once flat after the last one, with results unchanged (reference twin, plus a confined-month test).
- **Evidence:**
  - Every candidate trade is byte-identical to the engine trading that entry alone.
  - The mean random trade is within 0.03R of the hour- and side-matched mean, on normal and on long-holding data (the old rule misses by 0.093R there).
  - On random walk (slow test, 30 seeds) the no-edge percentile is uniform: mean 0.48, deciles [3, 5, 0, 3, 6, 1, 6, 0, 4, 2], and 2 of 30 at or above 0.95.

---

## T8: Prosecutor, Pass Bar, Verdict, Closest Miss

**Description:** The `prosecutor.py` checks from spec §6.3:
- the top-5 share;
- the neighbour-combination median PF, using the combination chosen for the Holdout and its ±1-step neighbours on the blind years;
- the side split (both sides, ≥ 30 trades each);
- ATR(14) terciles at entry (≥ 30 trades each).

`verdict.py` evaluates the ADR 0007 Pass Bar into a list of check results (name, value, threshold, passed, or n/a), the Verdict (CANDIDATE or REJECT) and the Closest Miss.

**Acceptance criteria:**
- [x] Each Prosecutor red flag fires on a hand-built input made to trip it, and stays quiet on one that doesn't. Checks with too few trades are reported as n/a, not as passed.
- [x] Any single failed check gives REJECT. A CANDIDATE needs every check passed or n/a, but PF, trade count, folds, stress, baseline and Deflated Sharpe can never be n/a.
- [x] The Closest Miss is the failed check whose value-to-threshold ratio is nearest 1, with a direction-aware ratio for "at most" checks such as PBO and the top-5 share.

**Verification:** `uv run pytest tests/test_verdict.py`

**Dependencies:** T3, T6

**Files (owned):** `src/vibedesk/gauntlet/prosecutor.py`, `src/vibedesk/gauntlet/verdict.py`, `tests/test_verdict.py`

**Scope:** M

**Result (2026-10-22, built here):**
- **API:**
  - `verdict.py`: `Check`, `judged`, `pass_bar(wf, baseline, deflated_sharpe, pbo)` (the core checks), `decide(checks) -> Verdict` and `CORE`.
  - `prosecutor.py`: `prosecute(wf)`, built from `concentration`, `neighbours`/`neighbour_indexes`, `sides` and `regimes`.
  - Statuses are `pass`, `fail` and `n/a`. `Check.to_dict()` is JSON-safe (NaN becomes null).
- **Choices made:**
  - The baseline check's value is the strategy's percentile, against 0.95. It passes only when net R is strictly above the 95th percentile.
  - Positive folds must be at least ceil(5/8 × folds).
  - Neighbours are the Combinations one step away on exactly one parameter axis from the Holdout's choice. The check uses their median blind PF; neighbours that never traded are ignored.
  - Regime terciles split the record's own entries by ATR(14) as a share of the close at the signal bar.
  - An n/a note is shown only when the check doesn't apply.
- **Deviation, accepted by the owner (2026-10-22):** the Closest Miss is chosen among failed **core** checks first. A Prosecutor flag is the Closest Miss only when every core check passed. Taken literally ("any failed check nearest 1"), random walk named the busy-regime split (0.85 vs 0.9) as its Closest Miss, while its PF was 0.77 vs 1.2.

---

## Checkpoint B (after T6–T8)

**Hand-run Trial (2026-10-22):** synthetic GOLD M15, 2014–2024, seed 1. Known-answer Spec `range_breakout(london_open)` + `atr_stop(1.5)` + `r_target(2)`, floor 55, 1,000 baseline runs; no Ledger and no Holdout yet.

| Data | Grid | Verdict | PF | Folds positive | Stress PF | Baseline percentile | DSR | PBO |
|---|---|---|---|---|---|---|---|---|
| Random walk | range_bars 4 | REJECT (Closest Miss: baseline) | 0.77 | 0 of 8 | 0.58 | 0.66 | 0.00 | n/a |
| Random walk | range_bars 2/4/8 | REJECT (Closest Miss: baseline) | 0.78 | 0 of 8 | 0.61 | 0.72 | 0.00 | 0.20 |
| Planted edge | range_bars 4 | CANDIDATE | 1.70 | 8 of 8 | 1.29 | 1.00 | 1.00 | n/a |
| Planted edge | range_bars 2/4/8 | CANDIDATE | 2.60 | 8 of 8 | 1.89 | 1.00 | 1.00 | 0.00 |

- **Speed:** a full 11-year M15 Trial (walk-forward, baseline, statistics, Prosecutor) took about 9 s for one Combination.
- **The 3-Combination grid** chose `range_bars` 2 in every fold on the planted edge; a 30-minute range catches more of the one-hour reveal.

**Findings after the hand-run, and the owner's decisions (2026-10-22):**
- **The baseline is biased at high occupancy.** At about 95% time in the market, fitting random trades one position at a time forces short trades. A no-edge Spec beat 100% of the runs. **Decision: independent draws** (see T7).
- **The Closest Miss is chosen among core checks first.** **Decision: accepted.** As a side effect, losing strategies often show the baseline as their Closest Miss: a percentile around 0.6 reads as nearer 0.95 than a PF of 0.77 does to 1.2.
- **Known-answer Spec, seeds and floors** (real Pass Bar, grid of 1, 1,000 runs of the earlier baseline rule; the hand-run Verdicts are unchanged under independent draws):

  | | Floor 55 | Floor 17 |
  |---|---|---|
  | Planted edge, seeds 1–5 | 4 of 5 CANDIDATE (seed 4 fails stress, 0.79) | 5 of 5 CANDIDATE |
  | Random walk, seeds 1–10 | 0 of 10 CANDIDATE | 0 of 10 CANDIDATE (PF 0.70–0.91) |

  **Decision: T10's known-answer test uses a 17-point Spread Floor.** That roughly matches real GOLD's cost relative to the synthetic prices. Production keeps the measured floor.
- **ma_cross with an opposite-signal exit never goes short** on a "both" Spec. **Decision: stop and reverse, as its own task after M2.** When the Spec allows that side, an opposite signal closes the trade and opens the other side at the same open.
- **Suite time:** the speed tests now share one twelve-year data fixture (`tests/conftest.py`). The default suite runs in 25–28 s.
- **T9 notes:**
  - Serialise the Trial record with `allow_nan=False`, and map an infinite PF explicitly, since browsers reject `Infinity` in JSON.
  - Seed the baseline from the Trial id.
  - Take the Deflated Sharpe's N and V from `ledger.deflation_inputs(family, pending_sharpes=wf.blind_sharpe)`.

- [x] The default suite is green: 779 passed, ruff clean (2026-10-22). Slow tests: uniform no-edge baseline (30 seeds) and planted-edge calibration pass.
- [x] A hand-run Trial on synthetic data prints a readable result (table above)
- [x] The owner reviewed the findings and decided all four questions (2026-10-22)

---

## T9: Trial runner + Holdout

**Description:** Three entry points:
- `run_trial(ledger, market, hypothesis_id, cost, m1=True)`:
  1. checks the Trial Budget;
  2. fetches the Research Window through `MarketData`;
  3. runs walk-forward, stress, baseline, Deflated Sharpe (with `ledger.deflation_inputs` including this Trial), PBO and the Prosecutor, then the Verdict;
  4. records the complete Trial.
- `request_holdout(ledger, hypothesis_id)` applies the gate: CANDIDATE only, and expected Holdout trades ≥ 30, else `INSUFFICIENT_TRADES` with nothing spent.
- `run_holdout(ledger, market, grant)` fetches all data with the grant, uses the 2022–2024 selection and trades entered from 2025-01-01, and returns PASS (PF ≥ 1.1 with ≥ 30 trades) or HOLDOUT FAIL.

**Acceptance criteria:**
- [x] Each `run_trial` adds exactly one Trial row, and every figure a Verdict Card needs can be read back from the stored record (rebuilt from the store in the test).
- [x] A 26th Trial is refused. A Holdout Request on a REJECT, or one with too few expected trades, is refused without spending. A second Holdout in the same family is refused.
- [x] The Research-Window fetch never reaches 2025, whatever the Spec asks for (asserted with a recording source).

**Verification:** `uv run pytest tests/test_trial.py`

**Dependencies:** T4, T5, T6, T7, T8

**Files (owned):** `src/vibedesk/gauntlet/trial.py`, `tests/test_trial.py`

**Scope:** M

**Result (2026-10-22, built here):**
- **API** (`gauntlet/trial.py`):
  - `run_trial(ledger, market, hypothesis_id, cost, *, use_m1=True, runs=1000, years=BLIND_YEARS) -> TrialResult`
  - `request_holdout(ledger, hypothesis_id, now=None) -> request_id`, which raises `HoldoutRefused` with reason `NOT_CANDIDATE` or `INSUFFICIENT_TRADES`
  - `run_holdout(ledger, market, grant, *, now=None) -> HoldoutResult`
  - `expected_holdout_trades(record, now)`
  - New Ledger read: `holdout_request(request_id) -> HoldoutRequest`, so a grant runs its own Hypothesis.
- **The stored record:** a strict JSON object, versioned, holding:
  - the Verdict, every check and the Closest Miss;
  - the CostSnapshot;
  - the walk-forward: choices, per-Combination per-fold results, blind Sharpe and PF, stitched and stress metrics, per-fold R, the daily-R equity series and every trade as columns;
  - the baseline: seed, runs, p95, percentile and the full net-R distribution for the histogram;
  - the Deflated Sharpe with its N and V, the PBO, and the Holdout trade rate.
- **JSON rules:** NaN is stored as null and infinity as `"inf"`, written with `allow_nan=False`. A test rebuilds the Verdict Card figures from the store alone.
- **Choices made:**
  - The baseline seed comes from (family, Hypothesis, Trial number in the family). The Trial id only exists after the insert, so the seed is stored with the Trial instead.
  - Budget and blind-year checks run before any bars are fetched; a recording source proves no fetch happens.
  - The Deflated Sharpe's N and V come from `deflation_inputs(..., pending_sharpes=wf.blind_sharpe)`, so a family's second Trial deflates by 2.
  - Expected Holdout trades = the Holdout Combination's blind trades per year × the years from 2025-01-01 to the request time.
  - `run_holdout` refuses, before fetching any bar, unless the Hypothesis's latest Trial is still a CANDIDATE (a Trial run since the request may have turned the Verdict). It then trades that Trial's chosen Combination under the Trial's stored CostSnapshot. It simulates from the first training year, so indicators are warm, and judges trades entered from 2025-01-01. A position still open from 2024 can block the first 2025 entries.
- **Tests (12, synthetic GOLD M15, floor 17):**
  - planted edge → CANDIDATE → Holdout PASS, spent once;
  - random walk → REJECT, its Holdout Request refused;
  - a too-early request is refused for `INSUFFICIENT_TRADES`;
  - a forced random-walk Holdout fails;
  - the 26th Trial is refused before any fetch;
  - Research-Window fetches all end by 2025-01-01.
- **Notes for M5 (the API and app):**
  - **A grant is a reusable key until the spend is recorded.** `verify` stays true until `record_holdout`, so anything holding the grant could fetch Holdout bars without spending it. A crash after the first Holdout fetch also leaves nothing spent, and a retry gets a second look. In M2 only `run_holdout` holds a grant. In M5, approving and running the Holdout should be one owner action inside the app, and the grant must never reach the Research Desk or the MCP server.
  - **Owner question for M5:** should a run that crashes after opening the Holdout count as spent? That would mean an "opened" row written before the first fetch.
  - **Bind each Holdout Request to the Trial id it was made from.** This needs a Ledger column.
  - **Trade rules can change between a Trial and its Holdout.** `run_holdout` re-simulates under whatever the engine does on the day it runs, so a Trial and its Holdout could be judged under different rules. Stop-and-reverse was the first such change; the time-filter decision may be the next. The fix is a `rules_version` stored in the Trial record, with `run_holdout` refusing on a mismatch. It must land before M3 produces real Trials.
  - **Concurrent Trials can share a seed.** `trial_number` is read outside the Ledger's write lock, so two Trials started at once on one family share a number and its baseline seed. The budget stays safe, because `record_trial` checks it under the lock.
- **Open for T10:** the default suite now takes about 40 s, against T10's 30 s bar. Options: run tests in parallel (pytest-xdist), or move the 12-year benchmarks and the heaviest statistical tests to `slow`.

---

## T10: Known answers + Trial benchmark

**Description:** The `slow` marker and `addopts` are already in `pyproject.toml` (done in T6). Two test files:
- **Known answers** (thresholds fixed in plan.md), using a 17-point Spread Floor (the owner's decision, 2026-10-22): the random walk gives at most 1 CANDIDATE in 20 seeds; the planted edge gives CANDIDATE in at least 4 of 5 seeds; and each planted-edge CANDIDATE that is put through the Holdout reaches PASS.
- **Benchmark:** the named 48-combination Trial takes ≤ 60 s. The worst case is measured and recorded.

**Acceptance criteria:**
- [x] `uv run pytest -m slow` passes all three known-answer checks.
- [x] The benchmark passes, and its timing breakdown (each Gauntlet stage) is written into this file.
- [x] The default `uv run pytest` stays under 30 s (about 24 s on 4 workers).

**Verification:** `uv run pytest -m slow`, `uv run pytest`

**Dependencies:** T2, T9

**Files (owned):** `pyproject.toml` (pytest options only), `tests/test_known_answers.py`, `tests/test_trial_perf.py`

**Scope:** M

**Result: known answers (2026-10-22, `tests/test_known_answers.py`, slow, 224 s).** Each Trial runs through `run_trial`, using production-shaped settings: blind years 2017–2024, M1 settling, 1,000 baseline runs, and a 17-point floor.

| | CANDIDATE | Bar | Detail |
|---|---|---|---|
| Random walk, seeds 101–120 (never used for calibration) | **0 of 20** | at most 1 | PF 0.43–0.89; baseline percentiles 0.02–0.90 |
| Planted edge, seeds 1–5 | **5 of 5** | at least 4 | PF 1.69–2.01; baseline percentile 1.00; no check failed |
| Planted-edge Holdout, 2025-01 to 2026-10 | **5 of 5 PASS** | every CANDIDATE | 552–576 trades, PF 1.72–2.31 |

**Result: benchmark and suite time (2026-10-22):**
- **Benchmark** (`tests/test_trial_perf.py`, slow): the full 48-Combination Trial through `run_trial` (Ledger included) on 11 research years of synthetic GOLD M15 with M1 took **4.3 s**, against a 60 s budget.
- **Stage breakdown, measured separately:**

  | Stage | Benchmark grid | Worst case |
  |---|---|---|
  | Walk-forward (compile, simulate, stress) | 1.4 s | 3.2 s |
  | Coin-Flip Baseline, 1,000 runs | 3.1 s | 3.8 s |
  | Deflated Sharpe | < 0.01 s | < 0.01 s |
  | PBO, 12,870 splits | 0.06 s | 0.04 s |
  | Prosecutor and Verdict | 0.01 s | 0.01 s |
  | **Total** | **4.6 s** | **7.1 s** |

  The worst case is 48 Combinations with every indicator near its bound: `bollinger_touch(period 350–500)`, `atr_regime(lookback 2000)`, `ma_slope(1000)`, ATR(200) stops and trailing. It was measured while the known-answer run loaded the CPU.
- **Default suite:** runs in parallel with pytest-xdist on 4 workers, one per physical core (owner's decision). It takes about 24 s for 793 tests. Eight workers (one per logical core) ran slower, about 40 s, and pushed the timing tests over their budgets. `-n 0` runs everything in one process.

---

## Checkpoint C: M2 done
- [x] Every M2 "done when" item in spec §10 is met: the known-answer tests pass (random walk 0 of 20, planted edge 5 of 5 with every Holdout PASS); a 48-Combination Trial takes 4.3 s against 60 s; the Holdout and Ledger guard tests pass.
- [x] `uv run pytest` (793 passed, about 24 s on 4 workers) and `uv run pytest -m slow` (all 6 slow tests, 200 s, peak Python memory 2.8 GB across 4 workers; the known-answer fixtures keep only small summaries) are green, and ruff is clean (2026-10-22).
- [x] The owner reviewed and approved Checkpoint C (2026-10-22)

**What the known answers do and don't prove (2026-10-22):**
- The random walk's 0 of 20 was decided by costs: every seed failed profit factor (0.43–0.89). The known-answer Spec is a grid of one, so the luck filters weren't really exercised there.
- **Lucky-grid measurement:** a no-edge 48-Combination H4 Donchian grid at zero cost (no spread, slippage or swap), over 30 random-walk seeds 201–230:
  - 29 of 30 already fail out of sample on PF, trades or folds, so walk-forward does most of the rejecting.
  - The one seed that cleared the naive bar (seed 208: PF 1.31, 101 trades, 5 of 8 folds positive) was stopped by the luck filters on six counts at once: baseline percentile 0.71, Deflated Sharpe 0.37, PBO 0.41, top-5 share, long-side PF and quiet-regime PF.
  - Result: 0 of 30 CANDIDATE.
  - One lucky survivor is thin evidence. A larger null run (more seeds, more grids) is worth doing once real ideas start flowing.

**Carried forward from M2:**
- **After M2:** opposite-signal stop-and-reverse (decided; built, see below).
- **Still open:** whether time filters judge the signal bar or the entry bar.
- **For M5 (the API and app):** the grant is reusable until the spend is recorded; bind each Holdout Request to its Trial id; concurrent Trials can share a seed.

---

## After M2: opposite-signal stop and reverse (2026-10-22, owner's decision)

**Rule (engine docstring, `opposite_signal` docstring):**
- The opposite exit is unchanged: when the entry Block alone signals the other way at a bar's close, the position leaves at the next open.
- **New:** when, at that same close, the Spec's own entry for the other side also fires (after its filters and direction, one-sided, with a usable stop), the position closes at the next open and the other side opens at that same open.
- The new position takes its stop and target from the signal bar and is checked on its entry bar like any entry. A time exit due at the same open takes priority as the closing reason; the reversal still happens.
- Without a valid entry on the other side (filtered out, a long-only Spec, warm-up), the opposite signal only closes.

**Knock-on changes:**
- **Walk-forward stitching:** a reversal (entry bar = the previous exit bar) survives a fold boundary only when both trades come from the same Combination's simulation. From two different simulations, the earlier trade may have left later in that bar, so the reversal is dropped.
- **Coin-Flip Baseline:** unchanged. Random trades closed by an opposite signal don't reverse, and each run already draws the strategy's full trade count, reversals included.
- **Invariant:** a trade may enter on the bar the previous one left only when it reverses (the other side) and the Spec has an opposite exit.

**Evidence:**
- **Golden by hand:** the long closes at 99.49 and the short enters at 99.49, with its stop at 103.49, closing at 100.11 (−0.155R). A London window that blocks the short's signal leaves only the close.
- **Reference twin** (written separately, with its own reversal logic): it agrees on the 200 random cases and on 40 reversal-heavy cases (`ma_cross` + opposite, with optional filter, time exit and trailing stop): 1,574 trades, 952 of them reversals. `ma_cross` + opposite now takes 737 shorts; before, it was effectively long-only.
- **End to end:** a full 4-Combination Trial on synthetic H1 gave 1,004 stitched trades (949 reversals, 501 shorts, no overlaps), a baseline percentile of 0.39 on random walk, and every trade stored.
- **Suites:** the default suite passes 837 tests in about 26 s; the slow suite passes all 6 tests in 211 s.

- [x] The owner reviews (approved 2026-10-22)
