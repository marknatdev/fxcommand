# M3 tasks: Market data from MT5, read-only

- **Spec:** [docs/spec/layer-a-verdict-engine.md](../docs/spec/layer-a-verdict-engine.md), §2, §3.3, §4, §9 and §10.
- **Plan and definitions:** [plan.md](plan.md). Tasks follow its "Definitions the tests depend on" exactly.
- **Commands:** `uv run pytest` (default suite), `uv run pytest -m slow` (known answers and benchmark), `uv run ruff check`, `uv run ruff format --check`.
- **Safety:** tests use `FakeMt5` only. No task may call an MT5 function outside the plan's allowlist; the guard tests enforce it.

**File ownership.** Each Phase 1 task owns its files and touches no one else's, so T1–T3 can run in parallel worktrees once T0 is committed. Shared files (`pyproject.toml`, `__init__.py` re-exports) change only in T0.

---

## T0: The contract: `Mt5Api`, `FakeMt5`, dependencies, the guard

**Description:** Fix the seam before any parallel work.
- `market/mt5_api.py`: an `Mt5Api` Protocol with exactly the allowed calls (`initialize`, `shutdown`, `last_error`, `symbol_info`, `symbol_info_tick`, `symbol_select`, `copy_rates_range`, `copy_ticks_range`) and the constants M3 uses. `SymbolInfo` and `Tick` as the fields M3 reads (point, digits, swap_long, swap_short, swap_mode, swap_rollover3days; time, time_msc, bid, ask).
- `market/fake_mt5.py`: `FakeMt5`, which serves `SyntheticBarSource` bars converted to server-clock time as MT5's structured arrays (`time`, `open`, `high`, `low`, `close`, `tick_volume`, `spread`, `real_volume`), ticks with a known spread distribution, and `symbol_info`. It can be told to return a partial chunk first (history still downloading) and to cap reads (the "Max bars" setting), so T4 can test both.
- `pyproject.toml`: `pyarrow`; `MetaTrader5` as an optional `mt5` extra for Windows; `[project.scripts] desk = "vibedesk.cli:main"` with a stub `cli.py` that prints its commands.
- Guard tests: the MT5 attribute allowlist (an AST scan of `src/`), and "only `market/mt5_api.py` imports MetaTrader5", through `real_api()` and `ReadOnlyMt5`.

**Acceptance criteria:**
- [x] `FakeMt5` satisfies `Mt5Api` (a structural check), and its bars round-trip to the `SyntheticBarSource` bars they came from once T1's conversion exists (that check lands in T1).
- [x] The allowlist guard fails on a planted `mt5.order_send` and on a planted `mt5.account_info`, and passes on `src/`.
- [x] `uv sync` works without the `mt5` extra; `uv run desk` prints the command list.

**Verification:** `uv run pytest tests/test_mt5_guard.py tests/test_fake_mt5.py`

**Dependencies:** none

**Files (owned):** `src/vibedesk/market/mt5_api.py`, `src/vibedesk/market/fake_mt5.py`, `src/vibedesk/cli.py` (stub), `pyproject.toml`, `uv.lock`, `tests/test_mt5_guard.py`, `tests/test_fake_mt5.py`

**Scope:** M

**Result (2026-10-23):**
- **The contract, from a read-only probe of the real terminal** (no prices printed): `copy_rates_range` and `copy_ticks_range` return the dtypes in `RATE_DTYPE` and `TICK_DTYPE`; a bound's UTC wall time is read as server wall time (a UTC 00:00 bound returned GOLD's first M15 bar at server 01:00, after its daily break); D1 bars open at server 00:00. The constants are copied from MetaTrader5 5.0.6231.
- **Three layers of read-only:**
  - the AST scan fails on any MT5 function off the allowlist, on any object, so `api.copy_rates_from_pos` is caught as well as `mt5.order_send`, plus any import of MetaTrader5 (also by `importlib` name) outside `mt5_api.py`;
  - `real_api()` returns the module wrapped in `ReadOnlyMt5`, which raises on every other name at run time;
  - with the extra installed, a test checks the scan's list of MT5 functions against the real module, and that every non-allowed public name raises.
- **`FakeMt5`:** server wall-clock times across a DST change (UTC+2 then UTC+3), D1 at server midnight, refuses naive bounds, `None` and `last_error()` before `initialize`, settling reads, a bar cap, a live clock (the forming bar), ticks with chosen spreads and missing asks, a stale tick at the weekend.
- **`desk`:** the entry point and a stub; each command says it arrives in T5.
- **Finding for T6:** at probe time (Friday 01:51 UTC) the terminal's last GOLD tick was about 19 hours old. Check the terminal is connected and receiving quotes before T6.
- **Suites:** the default suite passes 860 tests (1 skipped without the extra). `uv run --extra mt5 pytest tests/test_mt5_guard.py` runs the real-module check.

---

## T1: Server clock to UTC + `Mt5BarSource`

**Description:** Add `server_to_utc(server)` beside `server_offset(utc)` in `server_time.py`, as the plan defines it. Build `Mt5BarSource(api, symbols)`: a `BarSource` that widens a UTC range by a day, converts it to server-clock bounds, calls `copy_rates_range`, converts the times to UTC, trims to the range, and returns `Bars` with the symbol's point and digits. It drops MT5's extra columns.

**Acceptance criteria:**
- [ ] Round trip: utc → server → utc is the identity for every minute of 2014–2026, with every US DST weekend named in a test.
- [ ] Bars fetched from `FakeMt5` through `Mt5BarSource` equal the `SyntheticBarSource` bars exactly, for every timeframe, across a DST change.
- [ ] D1 bars convert to 17:00 New York opens and H4 bars to server hours divisible by 4 (a structural check that T6 reuses on real data).
- [ ] A range that ends inside a weekend or a holiday returns the right bars, with no off-by-one at either end.

**Verification:** `uv run pytest tests/test_server_time.py tests/test_mt5_source.py`

**Dependencies:** T0

**Files (owned):** `src/vibedesk/market/server_time.py`, `src/vibedesk/market/mt5.py`, `tests/test_server_time.py`, `tests/test_mt5_source.py`

**Scope:** M

---

## T2: Parquet cache + data-quality mask + `CachedBarSource`

**Description:** `market/cache.py`: write one year (or month) of `Bars` to `data/bars/{symbol}/{timeframe}/{year}.parquet` through a temporary file and a rename, and read a UTC range back across year files. `market/quality.py`: the first usable month and thin days, as the plan defines them, frozen from the Research Window and stored in `meta.json`. `CachedBarSource(root)`: a `BarSource` that reads the cache and applies the mask.

**Acceptance criteria:**
- [ ] Write then read gives back exactly the same `Bars`, including across year boundaries and for an empty year.
- [ ] A crash between the temporary write and the rename leaves the old file intact (a test that fails the rename).
- [ ] On synthetic data with a planted sparse start (one bar a day until a given month) and planted thin days, the mask finds exactly that month and exactly those days.
- [ ] A later refresh that adds days never changes the mask on old days, so a re-read of 2014–2024 is byte-for-byte the same.
- [ ] `MarketData(CachedBarSource(...), ledger)` keeps the Holdout guard: the existing guard tests pass with the cache behind the facade. A new guard: Parquet is read only in `market/cache.py`.
- [ ] The cache's per-year summary (the input to the data report) gives only bar counts, masked days, spreads and gaps for years from `HOLDOUT_START` on; a test checks it holds no price or return field for those years.

**Verification:** `uv run pytest tests/test_cache.py tests/test_quality.py tests/test_holdout_guard.py`

**Dependencies:** T0

**Files (owned):** `src/vibedesk/market/cache.py`, `src/vibedesk/market/quality.py`, `tests/test_cache.py`, `tests/test_quality.py`, `tests/test_holdout_guard.py`

**Scope:** M

---

## T3: Spread Floor + swap into a `CostSnapshot`

**Description:** `market/costs.py`: `measure_floor(api, symbol, now)` reads 14 days of ticks before `now` (`COPY_TICKS_INFO`), keeps ticks with both a bid and an ask, and returns the median, the 90th percentile and the count of `(ask − bid) / point`. `measure_cost(api, symbol, now)` adds the swap from `symbol_info`, refusing a `swap_mode` other than 1. Each measurement is appended to `data/costs/{symbol}.jsonl`; `current_cost(root, symbol)` returns the latest as a `CostSnapshot` with 1 point of slippage.

**Acceptance criteria:**
- [ ] On `FakeMt5` ticks with a known spread distribution, the median and p90 match a hand computation, and ticks missing a side are not counted.
- [ ] `swap_mode` 2 is refused with a message naming the symbol and the mode.
- [ ] The triple-swap day comes from `swap_rollover3days`, in the `CostSnapshot`'s numbering.
- [ ] `current_cost` returns the latest line; an older line is never rewritten.
- [ ] The snapshot's `to_json` round-trips, as the Ledger stores it.

**Verification:** `uv run pytest tests/test_market_costs.py`

**Dependencies:** T0

**Files (owned):** `src/vibedesk/market/costs.py`, `tests/test_market_costs.py`

**Scope:** S

---

## Checkpoint A (after T0–T3)
- [ ] Default suite green on the fakes, ruff clean
- [ ] The owner reviews

---

## T4: Importer and refresh

**Description:** `market/importer.py`: import every symbol and timeframe from an `Mt5BarSource` into the cache, chunk by chunk (M1 by month, others by year). Each import fixes a cutoff when it starts and keeps only bars closed by then; a chunk is accepted only after two reads agree on those closed bars. It builds the mask at the first import and extends it on refresh. `refresh` re-imports the current chunk and the one before it. One importer at a time, by a lock file. It returns a report: chunks read, bars per chunk, retries, capped chunks (counts only, no prices).

**Acceptance criteria:**
- [ ] A full import from `FakeMt5` into a temporary cache, read back through `MarketData`, equals the synthetic bars after the mask.
- [ ] A chunk that comes back short on the first read is re-read until two reads agree; a chunk that never settles fails the import with its symbol, timeframe and range.
- [ ] With a fake whose current chunk gains a new bar between reads (a live market), the chunk still settles, because only bars closed by the cutoff are compared.
- [ ] The bar still forming at the cutoff never reaches the cache, and the next refresh caches it once it has closed.
- [ ] A chunk that returns exactly the fake's cap is flagged in the report.
- [ ] A refresh rewrites only the current and the previous chunk's files (the other files' hashes are unchanged); a refresh just after New Year completes the old year's last day.
- [ ] A second importer started while one holds the lock is refused.
- [ ] Peak memory for a 12-year M1 import of one symbol stays under 1 GB (measured on the fake).

**Verification:** `uv run pytest tests/test_importer.py`

**Dependencies:** T1, T2

**Files (owned):** `src/vibedesk/market/importer.py`, `tests/test_importer.py`

**Scope:** M

---

## T5: `desk` commands

**Description:** Fill in `cli.py`: `desk smoke`, `desk import`, `desk refresh`, `desk floors` and `desk data-report`, as the plan defines them. The real `MetaTrader5` module is loaded only inside these commands; every command takes the API as a parameter so tests pass `FakeMt5`.

**Acceptance criteria:**
- [ ] `desk smoke` on `FakeMt5` prints the report and exits 0; with a fake that fails `initialize`, or a tick more than 120 s off, it exits non-zero with the reason. At a fake weekend the tick check is skipped and says so.
- [ ] `desk data-report` on a fake cache shows the first usable month, masked days per year and the clock checks per year; a fake year with a shifted clock fails.
- [ ] No command imports `MetaTrader5` when given a fake (checked with the module made unimportable).
- [ ] No command prints a price or a return for a date from `HOLDOUT_START` on: `desk smoke` prints counts and times, and the data report shows only structural facts for Holdout years (a test scans the output).

**Verification:** `uv run pytest tests/test_cli.py`

**Dependencies:** T3, T4

**Files (owned):** `src/vibedesk/cli.py`, `tests/test_cli.py`

**Scope:** M

---

## Checkpoint B (after T4–T5)
- [ ] A full import and refresh from `FakeMt5`, read back through `MarketData`, is green; ruff clean
- [ ] The owner reviews

---

## T6: Real import, data report, floors, smoke (here, read-only)

**Description:** Against the XM demo terminal: `desk smoke`, then `desk import` for all 8 symbols, `desk floors`, and `desk data-report`. Record the results here. Update spec §2 with anything the real data contradicts.

**Acceptance criteria:**
- [ ] `desk smoke` passes, with the live tick check run while the market is open.
- [ ] Every symbol and timeframe is in the cache from its first usable month; GOLD's first usable month is recorded.
- [ ] The structural clock checks pass for every year of every symbol, or a failing year is explained and handled before the checkpoint.
- [ ] Spread Floors and swaps are measured for all 8 symbols and compared with the probe (GOLD about 55 points, EURUSD about 20).
- [ ] Masked days per year are listed, with anything surprising explained.

**Verification:** the report, recorded below

**Dependencies:** T5

**Files:** `data/` (not committed), this file, spec §2

**Scope:** M

---

## T7: Timing and sanity on real data (records no result)

**Description:** Timing and sanity only (owner's decision, 2026-10-23). Run the Gauntlet with the M2 benchmark Spec (48 Combinations, GOLD M15, M1 settling, 1,000 baseline runs) on the real cache, into a Ledger in a temporary directory that is deleted afterwards. The script prints and records only the time and the sanity checks. It never prints or records the Verdict, a check's value, the profit factor or net R: an uncounted look at real results would undo ADR 0003's anti-reset rule.

**Acceptance criteria:**
- [ ] The run finishes in ≤ 60 s (spec §1).
- [ ] No NaN or infinity outside where the record allows it; every fold has bars; the baseline counts match the strategy's.
- [ ] The script's output and this file hold no Verdict, check value, profit factor or net R (a test runs the script's report function on a fake and scans it).
- [ ] The real `data/desk.db` is untouched.

**Verification:** the numbers, recorded below

**Dependencies:** T6

**Files:** this file

**Scope:** S

---

## Checkpoint C: M3 done
- [ ] Every M3 "done when" item in spec §10 is met
- [ ] `uv run pytest` and `uv run pytest -m slow` are green; ruff is clean
- [ ] The owner reviews
