# M2 tasks: Gauntlet and Ledger

- **Spec:** [docs/spec/layer-a-verdict-engine.md](../docs/spec/layer-a-verdict-engine.md), §4, §6, §9 and §10.
- **Plan and definitions:** [plan.md](plan.md). Tasks follow its "Definitions the tests depend on" exactly.
- **Commands:** `uv run pytest` (default suite), `uv run pytest -m slow` (known answers and benchmark), `uv run ruff check`, `uv run ruff format --check`.

**File ownership.** Each Phase 1 task owns its files and touches no one else's, so T1–T5 can run in parallel worktrees. Shared files (`pyproject.toml`, `__init__.py` re-exports) change only in the task named for them.

---

## T1: Prepared market + Block-output cache

**Description:** Hoist the per-call setup out of `simulate`. The new `prepare(bars, cost, m1=None) -> PreparedMarket` holds the bar arrays, the spread prices for bars and minutes, and the M1 index bounds. Add `simulate_prepared(market, signals)`. `simulate(bars, signals, cost, m1)` stays as a wrapper. `prepare(...)` with a stressed `CostSnapshot` gives the 2× spread market. Size `_run`'s output table by the number of entry signals plus one, not by the number of bars. Add a `BlockCache` to `compile_signals`, so a (block, params) pair is computed once per cache.

**Acceptance criteria:**
- [ ] On the 200 reference-twin seeds, `simulate_prepared` gives exactly the same records as `simulate`. The existing engine tests pass unchanged.
- [ ] On 12 years of synthetic GOLD M15 with M1, a prepared simulation of the `tests/test_perf.py` Spec takes ≤ 30 ms (best of 5).
- [ ] Compiling all 48 combinations of a grid that shares one `atr_regime(lookback=2000)` filter calls that filter exactly once (a counting test).

**Verification:** `uv run pytest tests/test_prepared.py tests/test_engine_props.py tests/test_engine_golden.py`

**Dependencies:** None

**Files (owned):** `src/vibedesk/engine/simulate.py`, `src/vibedesk/engine/prepared.py`, `src/vibedesk/blocks/compile.py`, `tests/test_prepared.py`

**Scope:** M

---

## T2: Synthetic planted edge + calibration (high risk; first)

**Description:** Replace the uncalibrated `trend` knob with a planted edge in entry timing (plan.md, "Known answers"). On each server day, draw a hidden direction. During the London opening hour, price drifts that way by `reveal` × hourly volatility. For the following `follow_hours` hours, it keeps drifting by `follow` × hourly volatility. Everything else stays a random walk. Calibrate `reveal` and `follow` empirically and write the numbers into this file.

**Acceptance criteria:**
- [ ] With the edge off, the known-answer Spec (`range_breakout(london_open, 4)`, `atr_stop(mult=1.5)`, `r_target(r=2)`, GOLD M15, Spread Floor 55) has a profit factor after costs below 1.05 on each of 5 seeds over 2014–2024.
- [ ] With the calibrated edge, the same Spec has a profit factor of at least 1.3 on each of 5 seeds over 2014–2024 and over 2025–2026. Random 50/50 entries at the same London hours with the same exits have a profit factor below 1.05.
- [ ] The edge never changes bar structure: the existing synthetic tests (aggregation, weekends, the point grid) still pass. With the edge off, the bars are the same as before.

**Verification:** `uv run pytest tests/test_synthetic.py`, plus a calibration script whose output is pasted here.

**Dependencies:** None

**Files (owned):** `src/vibedesk/market/synthetic.py`, `tests/test_synthetic.py`, `tests/test_planted_edge.py`

**Scope:** M

---

## T3: Statistics: Sharpe, Deflated Sharpe, PBO

**Description:** Pure functions in `gauntlet/stats.py`:
- `sharpe(daily_r)`: not annualised.
- `probabilistic_sharpe(sr, benchmark, t, skew, kurtosis)`.
- `deflated_sharpe(daily_r, n_trials, var_trial_sharpe)`, following Bailey & López de Prado, with non-excess kurtosis and the Euler–Mascheroni constant.
- `pbo(matrix)`: CSCV over 16 blocks, all 12,870 splits, vectorised.

Use `statistics.NormalDist`, with no SciPy dependency.

**Acceptance criteria:**
- [ ] The Deflated Sharpe limit cases hold:
  - N = 1 gives the probabilistic Sharpe against 0;
  - V[SR] = 0 gives a benchmark of 0;
  - it falls as N or V[SR] rises;
  - negative skew and fat tails lower it;
  - it matches a formula written separately in the test, on 50 random inputs.
- [ ] The PBO cases hold:
  - a combination that is best in every block gives 0;
  - IID noise (seed 0) gives 0.5 ± 0.1;
  - a best-in-sample that is worst out-of-sample gives ≥ 0.9;
  - all-zero blocks are handled;
  - a grid of 1 is reported as "n/a".
- [ ] PBO on a 48-combination × 16-block matrix takes ≤ 2 s.

**Verification:** `uv run pytest tests/test_stats.py`

**Dependencies:** None

**Files (owned):** `src/vibedesk/gauntlet/__init__.py`, `src/vibedesk/gauntlet/stats.py`, `tests/test_stats.py`

**Scope:** S

---

## T4: Market facade + Holdout guard

**Description:** `MarketData(source: BarSource, verifier: GrantVerifier)`, with `bars(symbol, tf, start, end, grant=None)`. It refuses any range reaching 2025-01-01 (the `HOLDOUT_START` constant) unless `verifier.verify(grant, symbol)` approves. `HoldoutGrant` is a frozen dataclass: request id, Idea Family id, token. `GrantVerifier` is a Protocol; the Ledger implements it in T5. A fake verifier serves the tests.

**Acceptance criteria:**
- [ ] Without a grant, any range ending after 2025-01-01 is refused with a clear error. Ranges ending on or before it work.
- [ ] A grant the verifier rejects (forged, wrong family, already spent) is refused. An approved grant opens the Holdout.
- [ ] A guard test fails if any module outside `vibedesk.market` calls `.fetch(` on a source.

**Verification:** `uv run pytest tests/test_holdout_guard.py`

**Dependencies:** None

**Files (owned):** `src/vibedesk/market/data.py`, `tests/test_holdout_guard.py`

**Scope:** S

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
- [ ] UPDATE and DELETE on hypotheses, trials and holdout spends abort, enforced by triggers. A code-scan test finds no UPDATE or DELETE statements outside the migrations.
- [ ] The Trial Budget is enforced. A 26th Trial is refused until the owner grants +25, and the grant is itself a ledger row. `deflation_inputs` sums combinations across all of the family's Trials.
- [ ] A Holdout grant verifies once for its own family and symbol, is refused after its spend is recorded, and is refused for any other family. A second spend in the same family violates the UNIQUE constraint. The anti-reset rule refuses a new family with the same entry Block and symbol.

**Verification:** `uv run pytest tests/test_ledger.py`

**Dependencies:** None (it implements T4's `GrantVerifier` Protocol by duck typing; the shapes are given here)

**Files (owned):** `src/vibedesk/ledger/__init__.py`, `src/vibedesk/ledger/store.py`, `tests/test_ledger.py`

**Scope:** M

---

## Checkpoint A (after T1–T5)
- [ ] `uv run pytest` and ruff are green
- [ ] T2's calibration numbers are recorded above
- [ ] The owner reviews before the Gauntlet core

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
