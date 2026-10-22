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
- [ ] On a hand-built grid of 3 combinations whose best combination changes by year, the choices per fold match the hand-worked answer. A trade that straddles the training boundary would flip one choice if it leaked, and the test shows it doesn't.
- [ ] The stitched record never holds two positions at once, and every trade keeps its own combination's exit rules (checked by recomputing each trade from that combination's own simulation).
- [ ] Stress uses the same fold choices, and each blind year's net R under stress is no better than without it.

**Verification:** `uv run pytest tests/test_walkforward.py`

**Dependencies:** T1

**Files (owned):** `src/vibedesk/gauntlet/walkforward.py`, `tests/test_walkforward.py`

**Scope:** M

---

## T7: Coin-Flip Baseline

**Description:** `coin_flip_baseline(walk_forward, market, runs=1000, seed)` returns the net-R distribution and its 95th percentile. Random entries follow the plan.md definition: per-year counts, the server-hour distribution, the long/short mix, each fold's exits, and deferral to a flat bar, with up to 20 redraws if entries spill past the year.

**Acceptance criteria:**
- [ ] The realised trade count per blind year equals the strategy's in every run of a 200-run test. Redraws are counted and reported.
- [ ] The entry-hour histogram over 1,000 runs matches the strategy's within ± 2 percentage points per hour. The long share matches within ± 2 points.
- [ ] Same seed, same distribution. On random-walk data, a random strategy's percentile against its own baseline is roughly uniform over 30 seeds (no bias).

**Verification:** `uv run pytest tests/test_baseline.py`

**Dependencies:** T1, T6

**Files (owned):** `src/vibedesk/gauntlet/baseline.py`, `tests/test_baseline.py`

**Scope:** M

---

## T8: Prosecutor, Pass Bar, Verdict, Closest Miss

**Description:** The `prosecutor.py` checks from spec §6.3:
- the top-5 share;
- the neighbour-combination median PF, using the combination chosen for the Holdout and its ±1-step neighbours on the blind years;
- the side split (both sides, ≥ 30 trades each);
- ATR(14) terciles at entry (≥ 30 trades each).

`verdict.py` evaluates the ADR 0007 Pass Bar into a list of check results (name, value, threshold, passed, or n/a), the Verdict (CANDIDATE or REJECT) and the Closest Miss.

**Acceptance criteria:**
- [ ] Each Prosecutor red flag fires on a hand-built input made to trip it, and stays quiet on one that doesn't. Checks with too few trades are reported as n/a, not as passed.
- [ ] Any single failed check gives REJECT. A CANDIDATE needs every check passed or n/a, but PF, trade count, folds, stress, baseline and Deflated Sharpe can never be n/a.
- [ ] The Closest Miss is the failed check whose value-to-threshold ratio is nearest 1, with a direction-aware ratio for "at most" checks such as PBO and the top-5 share.

**Verification:** `uv run pytest tests/test_verdict.py`

**Dependencies:** T3, T6

**Files (owned):** `src/vibedesk/gauntlet/prosecutor.py`, `src/vibedesk/gauntlet/verdict.py`, `tests/test_verdict.py`

**Scope:** M

---

## Checkpoint B (after T6–T8)
- [ ] The default suite is green
- [ ] A hand-run Trial on synthetic data prints a readable result
- [ ] The owner reviews

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
- [ ] Each `run_trial` adds exactly one Trial row, and every figure a Verdict Card needs can be read back from the stored record (rebuilt from the store in the test).
- [ ] A 26th Trial is refused. A Holdout Request on a REJECT, or one with too few expected trades, is refused without spending. A second Holdout in the same family is refused.
- [ ] The Research-Window fetch never reaches 2025, whatever the Spec asks for (asserted with a recording source).

**Verification:** `uv run pytest tests/test_trial.py`

**Dependencies:** T4, T5, T6, T7, T8

**Files (owned):** `src/vibedesk/gauntlet/trial.py`, `tests/test_trial.py`

**Scope:** M

---

## T10: Known answers + Trial benchmark

**Description:** Mark the slow tests and exclude them from the default suite. In `pyproject.toml`: `addopts = "-m 'not slow'"` and a `slow` marker. Then two test files:
- **Known answers** (thresholds fixed in plan.md): the random walk gives at most 1 CANDIDATE in 20 seeds; the planted edge gives CANDIDATE in at least 4 of 5 seeds; and each planted-edge CANDIDATE that is put through the Holdout reaches PASS.
- **Benchmark:** the named 48-combination Trial takes ≤ 60 s. The worst case is measured and recorded.

**Acceptance criteria:**
- [ ] `uv run pytest -m slow` passes all three known-answer checks.
- [ ] The benchmark passes, and its timing breakdown (each Gauntlet stage) is written into this file.
- [ ] The default `uv run pytest` stays under 30 s.

**Verification:** `uv run pytest -m slow`, `uv run pytest`

**Dependencies:** T2, T9

**Files (owned):** `pyproject.toml` (pytest options only), `tests/test_known_answers.py`, `tests/test_trial_perf.py`

**Scope:** M

---

## Checkpoint C: M2 done
- [ ] Every M2 "done when" item in spec §10 is met
- [ ] `uv run pytest` and `uv run pytest -m slow` are green, and ruff is clean
- [ ] The owner reviews before M3 (MT5 market data)
