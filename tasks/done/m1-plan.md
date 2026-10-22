# Implementation Plan: Layer A, Milestone M1 (pure core)

## Overview

M1 builds the pure core of the Verdict Engine: the `blocks` and `engine` modules from [the spec](../docs/spec/layer-a-verdict-engine.md) §3.2, §5 and §6.1. It runs entirely on `SyntheticBarSource`, with no MT5, no LLM and no network. M1 is done when a Strategy Spec composed from the Block Library v1 can be validated, expanded into its grid, compiled into signals and simulated into trades in R, with costs. Golden trades worked out by hand must match, the look-ahead property tests must pass, and one combination over 12 years of M15 must take ≤ 0.5 s.

## Architecture decisions for M1

- **Layout:**
  - one uv project at the repo root, Python 3.12;
  - package `src/vibedesk/`, tests in `tests/`;
  - the frontend comes later in `web/` (M5).
- **Bars:** an immutable container of numpy arrays: UTC epoch seconds, bid open/high/low/close, spread in points, and symbol metadata (point size, digits). Everything after it works on arrays; pandas is used only at the edges.
- **Blocks:**
  - Each Block is a pure function from (bars, params) to boolean or float arrays, and is registered with a pydantic parameter schema.
  - `compile()` combines one entry with its filters (AND) into long and short signal arrays, plus the exit parameters.
- **Engine:**
  - `simulate()` is one numba-jitted loop over bars, because fills are path-dependent.
  - Inputs: signal arrays, bars, optional M1 bars, and a `CostSnapshot`.
  - Output: an array of trades (entry and exit index, time and price, side, R, costs in R, exit reason).
- **Sessions:**
  - Exchange-local session windows are converted with `zoneinfo` from UTC bar times.
  - This is pure and covered by DST-edge tests.
  - The converter from server time to UTC is M3 work, since synthetic bars are already in UTC.
- **Guard tests from day one:**
  - §9.1, no order calls anywhere in `src/`;
  - §9.5, the look-ahead property test, which grows as Blocks are added.

## Task list

The detailed tasks are in [todo.md](todo.md).

### Phase 1: Tracer bullet
- [x] T1: Scaffold, the guard test, Bars and SyntheticBarSource
- [x] T2: Strategy Spec schema, validation and grid expansion
- [x] T3: First slice: `donchian_breakout` + `atr_stop` + `r_target` + `fixed_risk`, simulated into trades in R

### Checkpoint A (after T1–T3)
- [x] `uv run pytest` passes, `uv run ruff check` is clean
- [x] A golden hand-calculated trade list matches the engine exactly
- [x] The owner reviews the trade-list shape before more Blocks are built

### Phase 2: Complete the Cost Model and the Block Library
- [x] T4: Full Cost Model: short ask side, swap with Wednesday triple, settling same-bar conflicts with M1, CostSnapshot
- [x] T5: The remaining entry Blocks + the look-ahead property test over every Block
- [x] T6: Filter Blocks, including exchange-local sessions with DST tests

### Checkpoint B (after T4–T6)
- [x] Every entry and filter Block passes the look-ahead property test
- [x] Golden cost tests pass (spread floor, slippage, swap nights, triple Wednesday, M1 conflict)

### Phase 3: Exits, metrics, speed
- [x] T7: The remaining exits: `atr_target`, `atr_trailing`, `time_exit`, `opposite_signal`
- [x] T8: R metrics, the daily R series, the money-equity view, and the performance benchmark

### Checkpoint C: M1 done
- [x] All the M1 "done when" items in the spec §10 are met
- [x] One combination over 12 years of synthetic M15 data takes ≤ 0.5 s (benchmark test)
- [x] Review with the owner before starting M2 (gauntlet + ledger)

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Fill semantics bugs (gaps, same-bar stop and target, short ask side) quietly inflate results | High | Golden trades worked out by hand in T3, T4 and T7; hypothesis property tests (no trade has R above its target, a stop loss is never better than −1R minus costs) |
| Look-ahead leaking through indicators (rolling windows, session ranges) | High | The look-ahead property test runs on every Block (T5, T6) |
| numba compile time or type issues slow down tests | Medium | Cache the jit (`cache=True`); keep a pure-Python reference implementation and test that both give identical results on small inputs |
| Synthetic data too simple to show bugs that real data would | Medium | The synthetic generator includes gaps, weekend breaks, spread spikes and missing bars; a real-data check comes in M3 |
| Block param ranges let a 50-combination grid hide a large search | Low | Grid size enforced at validation (T2); the deflation in M2 counts every combination |

## Open questions

- None block M1. `git init` happens in T1; nothing is committed until the owner asks.
