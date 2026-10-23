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
- [x] Round trip: utc → server → utc is the identity for every minute of 2014–2026, with every US DST weekend named in a test (except the pinned server-Sunday minutes; see the result).
- [x] Bars fetched from `FakeMt5` through `Mt5BarSource` equal the `SyntheticBarSource` bars exactly, for every timeframe, across a DST change.
- [x] D1 bars convert to 17:00 New York opens and H4 bars to server hours divisible by 4 (a structural check that T6 reuses on real data).
- [x] A range that ends inside a weekend or a holiday returns the right bars, with no off-by-one at either end.

**Verification:** `uv run pytest tests/test_server_time.py tests/test_mt5_source.py`

**Dependencies:** T0

**Files (owned):** `src/vibedesk/market/server_time.py`, `src/vibedesk/market/mt5.py`, `tests/test_server_time.py`, `tests/test_mt5_source.py`

**Scope:** M

**Result (worktree session, merged 2026-10-23 as `3a58e3a`, branch commit `4ebe894`):**
- **`server_to_utc`:** the plan's formula, vectorised with one zoneinfo lookup per server hour. `check_clock({timeframe: utc_times})` returns, per UTC year and timeframe, the number of bars that break the rule.
- **`Mt5BarSource(api, *, clock=now)`:** selects the symbol once, takes point and digits from `symbol_info`, widens by a day, converts, trims to [start, end). A missing start means `RESEARCH_START`, a missing end `clock()`. The bar still forming at the end is included; T4's cutoff keeps closed bars only. The caller owns `initialize` and `shutdown`. Failures raise `Mt5Error` with `last_error()`.
- **Evidence:**
  - 6,837,120 minutes of 2014–2026 checked one year at a time; the only misses are exactly 26 × 420 = 10,920, the first seven UTC hours of each switch Sunday, all on server Sunday. The 26 switch dates are named and checked against the time-zone database. Every trading minute of each switch weekend round-trips exactly.
  - FakeMt5 through `Mt5BarSource` equals the source bars on all 5 timeframes for GOLD and EURUSD, across the 2025-11-02 and 2026-03-08 switches.
  - Clock checks: converted bars pass; a year shifted by 1 h fails D1 and H4, by 10 min all three.
  - Weekend, both switch Sundays, Christmas, a D1 week from Monday 00:00 UTC (4 bars, not 5), and 40 random ranges on every timeframe: no off-by-one.
- **Deviations:** the round trip (above; plan amended); no `symbols` parameter (names pass to MT5, which rejects unknown ones).
- **Finding, adopted into the plan:** the structural checks can't catch XM's clock having been set differently in the past. The plan now adds the XM clock checks (no weekend bars, 5 D1 bars a week, GOLD's daily break hour) for T5's data report and T6.

---

## T2: Parquet cache + data-quality mask + `CachedBarSource`

**Description:** `market/cache.py`: write one year (or month) of `Bars` to `data/bars/{symbol}/{timeframe}/{year}.parquet` through a temporary file and a rename, and read a UTC range back across year files. `market/quality.py`: the first usable month and thin days, as the plan defines them, frozen from the Research Window and stored in `meta.json`. `CachedBarSource(root)`: a `BarSource` that reads the cache and applies the mask.

**Acceptance criteria:**
- [x] Write then read gives back exactly the same `Bars`, including across year boundaries and for an empty year.
- [x] A crash between the temporary write and the rename leaves the old file intact (a test that fails the rename).
- [x] On synthetic data with a planted sparse start (one bar a day until a given month) and planted thin days, the mask finds exactly that month and exactly those days.
- [x] A later refresh that adds days never changes the mask on old days, so a re-read of 2014–2024 is byte-for-byte the same.
- [x] `MarketData(CachedBarSource(...), ledger)` keeps the Holdout guard: the existing guard tests pass with the cache behind the facade. A new guard: Parquet is read only in `market/cache.py`.
- [x] The cache's per-year summary (the input to the data report) gives only bar counts, masked days, spreads and gaps for years from `HOLDOUT_START` on; a test checks it holds no price or return field for those years.

**Verification:** `uv run pytest tests/test_cache.py tests/test_quality.py tests/test_holdout_guard.py`

**Dependencies:** T0

**Files (owned):** `src/vibedesk/market/cache.py`, `src/vibedesk/market/quality.py`, `tests/test_cache.py`, `tests/test_quality.py`, `tests/test_holdout_guard.py`

**Scope:** M

**Result (worktree session, merged 2026-10-23 as `4f446cb`, branch commit `3023acc`):**
- **API:** `write_chunk(root, bars, year, month=None)` replaces one UTC year (or a month of it, merged into the year file); `record_import(root, symbol, cutoff)` judges the server days that closed by the cutoff (the first call builds and freezes the mask); `CachedBarSource(root)`; `read_unmasked` (guarded like `.fetch(`); `read_meta`; `summarize(root, symbol)`. `root` is the data folder, with the cache under `root/bars/`.
- **Evidence:**
  - round trips on all 5 timeframes across New Year and for empty chunks;
  - a failed `os.replace` leaves the year file and `meta.json` byte-identical, with no temporary file left;
  - the planted case gives median 96, first usable month 2024-02 and exactly the planted thin days (47 bars masked, 48 kept);
  - a refresh that backfills and adds days leaves 2014–2024 byte-identical;
  - every facade test also runs over the cache;
  - two caches that differ only in Holdout prices give identical summaries;
  - three deliberate breaks (median over all days, mask rebuilt per import, write without the temporary file) were each caught.
- **Choices beyond the plan:**
  - A server day is served only once an import has judged it, so a bar once served is never taken back. Today's bars wait for the next import.
  - `meta.json` also stores the frozen median and `judged_through`.
  - Thin days are judged only from the first usable month on.
  - A gap is 2 hours or more with no bar inside one server week (weekends are no gap). The summary is structural for every year, read from `time` and `spread` only.
  - The Parquet guard is broad: outside `market/cache.py`, no pyarrow, pandas, polars, duckdb or fastparquet import, no Parquet reader names, and no `.parquet` string (docstrings excepted).

---

## T3: Spread Floor + swap into a `CostSnapshot`

**Description:** `market/costs.py`: `measure_floor(api, symbol, now)` reads 14 days of ticks before `now` (`COPY_TICKS_INFO`), keeps ticks with both a bid and an ask, and returns the median, the 90th percentile and the count of `(ask − bid) / point`. `measure_cost(api, symbol, now)` adds the swap from `symbol_info`, refusing a `swap_mode` other than 1. Each measurement is appended to `data/costs/{symbol}.jsonl`; `current_cost(root, symbol)` returns the latest as a `CostSnapshot` with 1 point of slippage.

**Acceptance criteria:**
- [x] On `FakeMt5` ticks with a known spread distribution, the median and p90 match a hand computation, and ticks missing a side are not counted.
- [x] `swap_mode` 2 is refused with a message naming the symbol and the mode.
- [x] The triple-swap day comes from `swap_rollover3days`, in the `CostSnapshot`'s numbering.
- [x] `current_cost` returns the latest line; an older line is never rewritten.
- [x] The snapshot's `to_json` round-trips, as the Ledger stores it.

**Verification:** `uv run pytest tests/test_market_costs.py`

**Dependencies:** T0

**Files (owned):** `src/vibedesk/market/costs.py`, `tests/test_market_costs.py`

**Scope:** S

**Result (worktree session, merged 2026-10-23 as `b5b121b`, branch commit `bb8f575`):**
- **API:** `measure_floor`, `measure_cost` (writes nothing), `record_cost(root, measurement)` (appends), `current_cost(root, symbol)`. Failures raise `CostError`, whose message starts with the symbol; a naive `now` raises `ValueError`.
- **Evidence:**
  - two whole weeks across the 8 March switch, with a cycle of spreads whose 100s miss their ask: 25,920 ticks, median 55, p90 70 (counting the ask-less ticks would give 52.5 and 61); missing bids are ignored too;
  - swap_mode 2 is refused before any tick is read;
  - triple-swap day 3 gives 3 nights Wed→Thu and 1 Fri→Mon (day 5 the reverse); day 7 is refused;
  - appends leave the earlier bytes as an unchanged prefix, and a stored line holds no price;
  - seven deliberate breaks were each caught.
- **Notes:** the triple-swap mapping is the identity (both number days from Sunday = 0); spreads are rounded to whole points before the median; `symbol_select` is not called here, so the CLI selects each symbol before measuring.

---

## Checkpoint A (after T0–T3)
- [x] Default suite green on the fakes, ruff clean: a trial merge of T1–T3 passed 1045 tests (with the timing tests) and the 6 slow tests (203 s), 2026-10-23
- [x] The owner reviews (merge approved 2026-10-23)

**Contract fixes at merge:** `mt5_api.py`'s docstring named the wrong importing module (it is `mt5_api.py` itself, in `real_api()`); `FakeMt5` takes any `BarSource`, not only a `SyntheticBarSource`.

---

## T4: Importer and refresh

**Description:** `market/importer.py`: import every symbol and timeframe from an `Mt5BarSource` into the cache, chunk by chunk (M1 by month, others by year). Each import fixes a cutoff when it starts and keeps only bars closed by then; a chunk is accepted only after two reads agree on those closed bars. It builds the mask at the first import and extends it on refresh. `refresh` re-imports the current chunk and the one before it. One importer at a time, by a lock file. It returns a report: chunks read, bars per chunk, retries, capped chunks (counts only, no prices).

**From T1 and T2:** read with `Mt5BarSource` (it includes the forming bar; the cutoff drops it), write with `write_chunk`, and call `record_import(root, symbol, cutoff)` once, after every chunk of the import has settled. `CachedBarSource` withholds server days not yet judged, so a read-back ends at the last server day closed by the cutoff. The Parquet guard forbids naming Parquet files in `src/` outside `cache.py`; tests that hash files compute the paths themselves.

**Acceptance criteria:**
- [x] A full import from `FakeMt5` into a temporary cache, read back through `MarketData`, equals the synthetic bars after the mask.
- [x] A chunk that comes back short on the first read is re-read until two reads agree; a chunk that never settles fails the import with its symbol, timeframe and range.
- [x] With a fake whose current chunk gains a new bar between reads (a live market), the chunk still settles, because only bars closed by the cutoff are compared.
- [x] The bar still forming at the cutoff never reaches the cache, and the next refresh caches it once it has closed.
- [x] A chunk that returns exactly the fake's cap is flagged in the report.
- [x] A refresh rewrites only the current and the previous chunk's files (the other files' hashes are unchanged); a refresh just after New Year completes the old year's last day.
- [x] A second importer started while one holds the lock is refused.
- [x] Peak memory for a 12-year M1 import of one symbol stays under 1 GB (measured on the fake).

**Verification:** `uv run pytest tests/test_importer.py`

**Dependencies:** T1, T2

**Files (owned):** `src/vibedesk/market/importer.py`, `tests/test_importer.py`

**Scope:** M

**Result (built here, 2026-10-23; awaiting review):**
- **API:** `import_history(api, root, *, symbols=UNIVERSE, timeframes=TIMEFRAMES, start=RESEARCH_START, clock)` and `refresh(api, root, *, symbols, timeframes, clock)` return an `ImportReport`: the clock's cutoff; per chunk bars, reads and flag; per symbol its own cutoff, `judged_through` and whether this run judged days. Counts only. `import_lock(root)` holds `bars/import.lock`. Failures raise `ImportFailed` (`ImportLocked` for the lock). The importer owns `initialize` and `shutdown`.
- **How it works:** the cutoff is fixed at the start and every read is cut to the bars closed by it; a chunk is accepted when two reads in a row are identical (every column, not just count and last time), and fails after `MAX_READS` = 6. `record_import` runs per symbol only after all its chunks settled, so a failed import serves nothing new. An empty chunk is written only to clear bars the cache held there.
- **Evidence (`tests/test_importer.py`, 17 tests; default suite 1061 passed, slow suite 7 passed in 219 s):**
  - a full import of four months of GOLD on all 5 timeframes reads back through `MarketData` exactly as the synthetic bars after the mask, closed by the cutoff; 12 chunks, all settled in 2 reads, nothing flagged;
  - a downloading terminal takes 3 reads per chunk and caches the same bars as a clean one; one that never settles fails as "GOLD M1 2023-11 (2023-11-01 to …) did not settle", with no mask, nothing served and the lock released;
  - a live terminal clock 15 minutes on at every call: the raw reads differ, the chunk still settles in 2 reads, and the 10:00 bar forming at the 10:07 cutoff is not cached; a refresh at 10:20 caches it (M1 through 10:19);
  - a 20,000-bar cap flags exactly the M1 months after the first ("starts late: a capped read or a gap");
  - a refresh at 2 January 12:00 after an import at 31 December 12:00 writes exactly M1 2024-12 and 2025-01 and the 2024 and 2025 years of the others (no D1 bar of 2025 had closed yet, so none is written), leaves the 2023 files' bytes and modification times unchanged, completes 31 December's M15 bars, and judges days through 1 January;
  - a second importer is refused while the lock is held, and runs once it is released;
  - refusals before any read: a refresh before any import, a first import without M15, a failed `initialize`, a naive clock;
  - **memory (slow test):** a 12-year M1 import of one symbol (4.8 million bars, with M15) peaks at 399 MiB traced.
  - the three guards below, each with a test; breaking each guard in turn (no tick cap, no judged-day check, judging without M15, ignoring a short M15) fails its test.
- **Deviations from the plan, for review:**
  - **Refresh range:** from the earlier of the chunk before the cutoff's and the chunk of the last import, through the cutoff's chunk. This is the plan's "current and previous" when refreshes run often, and also fills the gap after a refresh missed for months.
  - **Capped reads are flagged by shape, not by count:** `Mt5BarSource` trims what MT5 returns, so a cap can't be seen as an exact bar count. A chunk after earlier data that is empty or starts more than 4 days late is flagged.
  - **New: a stalled terminal.** Testing found that a terminal stuck half way through a download returns the same short read twice, which two agreeing reads accept. The importer now also flags a finished chunk whose last bar closes more than 4 days before its end ("a read cut short or a gap"), and `FakeMt5`'s settling reads now grow on every read, as a downloading terminal's do. A test with a stalled fake shows every short chunk flagged. Flags are warnings in the report; T5's import command should print them, and T6 must explain any it sees.
  - **Three guards keep a bad read out of what Trials see** (added after review of the first draft, 2026-10-23):
    1. **A symbol's cutoff is the earlier of the clock and its last tick** (`symbol_info_tick`, converted with `server_to_utc`). A terminal that stopped receiving quotes would otherwise have its silent hours judged: a partial day masked for good, an empty day never judged and later served unjudged. Test: a terminal stuck at Tuesday 17:00 with the clock at Wednesday noon judges through Monday and leaves Tuesday unmasked. On a weekend the last tick is Friday's close, so Friday is judged at the next import. A symbol with no tick fails. Chunks that start after the last tick are skipped, not read: the slow memory test caught them being flagged as short and blocking the symbol, and a fast test now covers it.
    2. **Days are judged only when the run imported M15, and never after a short M15 chunk.** A refresh of H1 alone reports `judged=False` and leaves `judged_through` where it was; a flagged M15 chunk fails the symbol, naming the chunk, before any day is judged (a stalled first import leaves no mask, so nothing is served).
    3. **No chunk may change bars on a server day already judged.** Before writing, the chunk's bars on judged days are compared with the cache's; any difference fails the import, naming the chunk and the first changed day, and nothing is written for it. A refresh through a stalled terminal fails as "GOLD M1 2024-01 would change bars on 2024-01-1…", with every 2023 and 2024 file's bytes and modification time unchanged. This also stops a genuine MT5 history revision from silently changing a Trial's bars; the owner hears about it instead.

---

## T5: `desk` commands

**Description:** Fill in `cli.py`: `desk smoke`, `desk import`, `desk refresh`, `desk floors` and `desk data-report`, as the plan defines them. The real `MetaTrader5` module is loaded only inside these commands; every command takes the API as a parameter so tests pass `FakeMt5`.
- `desk floors`: select each symbol, then `record_cost(root, measure_cost(api, symbol, now))`; report a `CostError` per symbol.
- `desk data-report`: joins T2's `summarize`, T1's `check_clock` and the plan's XM clock checks (no bar on a server Saturday or Sunday, 5 D1 bars per full week, GOLD's daily break at the same server hour in US summer and winter), computed inside `market/` from bar times only.

**Acceptance criteria:**
- [ ] `desk smoke` on `FakeMt5` prints the report and exits 0; with a fake that fails `initialize`, or a tick more than 120 s off, it exits non-zero with the reason. At a fake weekend the tick check is skipped and says so.
- [ ] `desk data-report` on a fake cache shows the first usable month, masked days per year and the clock checks per year; a fake year with a shifted clock fails.
- [ ] A fake year built on a fixed GMT+2 server clock (no US daylight saving) fails the XM clock checks: weekend bars in US summer, and GOLD's break moving by an hour.
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
- [ ] The structural clock checks and the XM clock checks pass for every year of every symbol, or a failing year is explained and handled before the checkpoint.
- [ ] Before importing, the terminal is connected and receiving quotes (T0 found its last GOLD tick about 19 hours old); a stale terminal would freeze partial days as masked.
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
