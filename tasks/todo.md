# M1 tasks: the pure core (blocks and engine)

- **Spec:** [docs/spec/layer-a-verdict-engine.md](../docs/spec/layer-a-verdict-engine.md)
- **Plan:** [plan.md](plan.md)
- **Commands:** `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`

---

## T1: Scaffold, guard test, Bars, SyntheticBarSource

**Description:** Create the uv project (Python 3.12) with the `src/vibedesk/` layout, ruff and pytest, and run `git init`. Add the §9.1 guard test, which scans `src/` and fails on any order-related MT5 call. Add the `Bars` container and a seeded `SyntheticBarSource` that can generate the following:
- a random walk with weekend gaps, missing bars and spread spikes;
- a planted-edge series, for later known-answer tests.

**Acceptance criteria:**
- [x] `uv run pytest` passes on a fresh clone. The guard test fails when a file containing `order_send` is temporarily added (shown by a test fixture).
- [x] `Bars` is immutable, validates that its arrays have equal length and that times are monotonic UTC, and exposes `point` and `digits`.
- [x] `SyntheticBarSource(seed)` returns identical bars for identical seeds and supports M1, M15, H1, H4 and D1, with the M1 bars aggregating exactly to the higher timeframes.

**Verification:** `uv run pytest tests/test_guard.py tests/test_bars.py tests/test_synthetic.py`

**Dependencies:** None

**Files:** `pyproject.toml`, `.gitignore`, `src/vibedesk/market/bars.py`, `src/vibedesk/market/synthetic.py`, `tests/test_guard.py`, `tests/test_bars.py`, `tests/test_synthetic.py`

**Scope:** M

---

## T2: Strategy Spec schema, validation, grid expansion

**Description:** Write the pydantic models for the Strategy Spec in spec §5.1 and a Block registry where each Block declares its kind, its parameter schema and its implementation. Validation enforces these rules:
- every Block named in the Spec is known;
- parameter types and ranges are valid;
- an exit stop is present;
- `direction` is one of `long`, `short` or `both`;
- the grid has at most 50 combinations.

`expand_grid()` yields every concrete parameter set in a stable order.

**Acceptance criteria:**
- [x] The example Spec from §5.1 validates and expands into exactly 2 × 3 × 3 × 2 = 36 combinations. A variant with a third `r` value (54 combinations) is rejected with a clear error about the grid limit.
- [x] Specs that fail validation return structured errors (a path and a reason) that the Research Desk can read.
- [x] Grid order is deterministic: the same Spec always yields the same list of combinations.

**Verification:** `uv run pytest tests/test_spec.py`

**Dependencies:** T1

**Files:** `src/vibedesk/blocks/spec.py`, `src/vibedesk/blocks/registry.py`, `tests/test_spec.py`

**Scope:** S

---

## T3: Tracer bullet: Donchian breakout to trades in R

**Description:** Implement `donchian_breakout`, `atr_stop`, `r_target` and `fixed_risk`, along with `compile()` and a first `simulate()`:
- the signal fires on the bar's close and the trade enters at the next bar's open plus spread;
- spread is max(bar spread, floor), with 1 point of slippage per side;
- stops and targets are checked inside each bar, and the stop wins when both are touched in the same bar;
- at most one open position at a time.

Each trade records its R and its costs in R. Write a pure-Python reference version and a numba version.

**Acceptance criteria:**
- [x] On a 40-bar hand-built series, the engine's trade list matches a golden list worked out by hand: entry and exit times and prices, R, and exit reasons, including one gap through the stop and one bar that touches both stop and target.
- [x] The numba and reference versions give identical results on 200 random synthetic seeds.
- [x] Property tests: no losing trade is better than −1R minus its cost, no winning trade exceeds its target R, and there is never more than one open position.

**Verification:** `uv run pytest tests/test_engine_golden.py tests/test_engine_props.py`

**Dependencies:** T2

**Files:** `src/vibedesk/blocks/entries.py`, `src/vibedesk/blocks/exits.py`, `src/vibedesk/blocks/compile.py`, `src/vibedesk/engine/simulate.py`, `src/vibedesk/engine/costs.py`, `tests/test_engine_golden.py`, `tests/test_engine_props.py`

**Scope:** M

---

## Checkpoint A (after T1–T3)

**Deviations from the plan (2026-10-22):**
- **Short side in T3:** the short side (bid entries, ask-side stops and targets) landed in T3, because the fill loop is symmetric. T4 is left with swap, settling same-bar conflicts with M1, CostSnapshot JSON and `as_of`, and stress mode.
- **Reference engine location:** the independent reference engine lives in `tests/reference_engine.py`, not `src/`. It shares no code with the engine.
- **Extra files:**
  - `market/source.py`: the BarSource seam.
  - `market/server_time.py`: server clock = New York + 7 h.
  - `blocks/sizing.py`: `fixed_risk`, capped at 1%.
- **Property wording made precise:**
  - a plain stop is exactly −1R minus exit slippage;
  - only a gap loses more than that;
  - a target exit is exactly target R minus slippage;
  - no trade exceeds its target.
- **Bug found and fixed before the checkpoint:** a stop closer than spread + slippage was already breached at the entry open, but it was filled at the stop price. In the 200 random cases, 463 of 5,945 trades (7.8%) were booked as −1R stops, understating the loss by up to 1.38R. The entry bar now exits at the market as `STOP_GAP`. Covered by `tests/test_engine_inside_spread.py` and a new invariant.

- [x] All tests pass and ruff is clean (259 tests)
- [x] The golden trade list matches
- [ ] The owner reviews the trade-record shape

---

## T4: Full Cost Model

**Description:** Complete the Cost Model in spec §6.1:
- shorts enter at bid and exit at ask, and their stops and targets are checked against ask-side highs and lows;
- swap is charged in points per lot for each night held at the server rollover, ×3 on the Wednesday rollover, then converted to R;
- when M1 bars are supplied, a bar that touches both stop and target is settled by them, and the stop wins when M1 is missing or still ambiguous;
- an immutable `CostSnapshot` (floor, swap long and short, slippage, point size, `as_of`) is attached to every result.

**Acceptance criteria:**
- [ ] Golden tests cover a short trade on the ask side, a 3-night hold that crosses Wednesday (charged 5 swap nights), and a same-bar stop-and-target conflict settled both ways by M1.
- [ ] Doubling the floor and bar spreads (stress mode) never improves any trade's R.
- [ ] A `CostSnapshot` round-trips through JSON unchanged.

**Verification:** `uv run pytest tests/test_costs.py`

**Dependencies:** T3

**Files:** `src/vibedesk/engine/costs.py`, `src/vibedesk/engine/simulate.py`, `tests/test_costs.py`

**Scope:** M

---

## T5: Remaining entry Blocks + look-ahead property test

**Description:** Implement `ma_cross`, `rsi_threshold`, `bollinger_touch`, `pullback_to_ma` and `range_breakout`, which builds its range in exchange-local time. Add a generic look-ahead property test that runs over every registered Block. It randomly changes the bars after index *t* and asserts that the Block's output at index ≤ *t* is unchanged.

**Acceptance criteria:**
- [ ] Every entry Block passes the look-ahead property test on 100 random seeds and cut points.
- [ ] Each Block has at least one small hand-checked test of its signal positions.
- [ ] `range_breakout`'s London-open range keeps the right UTC bar times across both the UK and US daylight-saving switches (the weeks in March and October/November when only one of them has switched).

**Verification:** `uv run pytest tests/test_entries.py tests/test_lookahead.py`

**Dependencies:** T3

**Files:** `src/vibedesk/blocks/entries.py`, `src/vibedesk/blocks/indicators.py`, `src/vibedesk/blocks/sessions.py`, `tests/test_entries.py`, `tests/test_lookahead.py`

**Scope:** M

---

## T6: Filter Blocks

**Description:** Implement `ma_slope`, `adx`, `atr_regime` (with a trailing percentile only), `session_window` and `day_of_week`, and have `compile()` combine them with the entry using AND. All of them are added to the look-ahead property test.

**Acceptance criteria:**
- [ ] Every filter passes the look-ahead property test.
- [ ] `atr_regime`'s percentile uses only past bars; a hand-checked test covers the boundary.
- [ ] An entry combined with a filter fires only where both are true, checked on a small hand-built series.

**Verification:** `uv run pytest tests/test_filters.py tests/test_lookahead.py`

**Dependencies:** T5

**Files:** `src/vibedesk/blocks/filters.py`, `src/vibedesk/blocks/compile.py`, `tests/test_filters.py`

**Scope:** M

---

## Checkpoint B (after T4–T6)
- [ ] The look-ahead property test is green for every entry and filter Block
- [ ] The golden cost tests are green

---

## T7: Remaining exits

**Description:** Add four exits to the engine:
- `atr_target`
- `atr_trailing`: ratchets only in the trade's favour and is updated only on bar closes
- `time_exit`: exits at the open after N bars
- `opposite_signal`: exits at the next open after an opposite entry signal

When several exits apply, the order is stop → target → trailing → time → opposite. This is documented and tested.

**Acceptance criteria:**
- [ ] Golden tests cover each exit, plus one trade where a trailing stop and a time exit compete.
- [ ] The trailing stop never moves against the trade (property test).
- [ ] The numba and reference versions stay identical on 200 seeds with every exit enabled.

**Verification:** `uv run pytest tests/test_exits.py tests/test_engine_props.py`

**Dependencies:** T4

**Files:** `src/vibedesk/blocks/exits.py`, `src/vibedesk/engine/simulate.py`, `tests/test_exits.py`

**Scope:** M

---

## T8: Metrics in R, daily series, money view, benchmark

**Description:** Build `metrics(trades, bars)`. It returns these figures:
- trade count, profit factor, win rate (shown only, never judged), expectancy in R and net R;
- the largest share of net R made by the top 5 trades;
- the **daily R series**, one value for every trading day including zero days;
- maximum drawdown and the equity curve at 1% risk on $1,000.

Add a benchmark test: one combination over 12 years of synthetic M15 data, with the numba version already compiled, takes ≤ 0.5 s.

**Acceptance criteria:**
- [ ] Metrics match hand-calculated values on the golden trade list from T3 and T4.
- [ ] The daily R series sums to net R and has one row for every trading day in the range.
- [ ] The benchmark test passes. It is marked `@pytest.mark.perf` and runs in the default suite.

**Verification:** `uv run pytest tests/test_metrics.py tests/test_perf.py`

**Dependencies:** T7

**Files:** `src/vibedesk/engine/metrics.py`, `tests/test_metrics.py`, `tests/test_perf.py`

**Scope:** S

---

## Checkpoint C: M1 done
- [ ] Every M1 "done when" item in spec §10 is met
- [ ] The full suite passes and ruff is clean
- [ ] The owner reviews before M2 (gauntlet and ledger)
