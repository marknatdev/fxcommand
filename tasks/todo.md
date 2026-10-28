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
- [x] `desk smoke` on `FakeMt5` prints the report and exits 0; with a fake that fails `initialize`, or a tick more than 120 s off, it exits non-zero with the reason. At a fake weekend the tick check is skipped and says so.
- [x] `desk data-report` on a fake cache shows the first usable month, masked days per year and the clock checks per year; a fake year with a shifted clock fails.
- [x] A fake year built on a fixed GMT+2 server clock (no US daylight saving) fails the XM clock checks: weekend bars in US summer, and GOLD's break moving by an hour. (FX shows the weekend bars, GOLD the break; see the deviations.)
- [x] No command imports `MetaTrader5` when given a fake (checked with the module made unimportable).
- [x] No command prints a price or a return for a date from `HOLDOUT_START` on: `desk smoke` prints counts and times, and the data report shows only structural facts for Holdout years (a test scans the output).

**Verification:** `uv run pytest tests/test_cli.py`

**Dependencies:** T3, T4

**Files (owned):** `src/vibedesk/cli.py`, `tests/test_cli.py`

**Scope:** M

**Result (built here, 2026-10-23; awaiting review at Checkpoint B):**
- **Commands:** `uv run desk smoke | import | refresh | floors | data-report`. Each takes `--symbols` (default all 8); all but `smoke` take `--data` (default the repo's `data/`); `import` takes `--start` (default `RESEARCH_START`). Exit 0 when everything passed, 1 when a check failed or a command could not finish, 2 for bad usage. `main(argv, api=, clock=)` takes the API and the clock, so tests pass `FakeMt5`; the live terminal is loaded only when a command needs one, and without the mt5 extra a command says `uv sync --extra mt5` instead of a traceback.
  - `smoke`: `initialize`; per symbol `symbol_select`, then only named fields that aren't prices (point, digits, swap mode, triple-swap day); the live tick check; a day of M15 bars (count and last open time) and an hour of ticks (count); `shutdown`.
  - `import` and `refresh`: T4's importer; per symbol chunks, bars, retries, cutoff and judged-through, then every flagged chunk. A failure prints `FAILED:` and the importer's reason.
  - `floors`: `measure_cost` then `record_cost` per symbol; prints the floor, p90 and tick count, the measured swaps and the charged swaps (a credit charged as 0, ADR 0004). A `CostError` fails that symbol; the others carry on.
  - `data-report`: reads the cache only, never the terminal. Per symbol: first usable month, judged through, last import. Per year: bars per timeframe, masked days split into thin days (some M15 bars) and empty weekdays (none), gaps, median spread, our conversion (`check_clock`) and XM's clock.
- **XM's clock** (`market/report.py`, from bar times only): bars (M15 and D1) opening on a server Saturday or Sunday; server weeks with more than 5 D1 bars; the daily break, the server hours with M15 bars on fewer than 10% of Tuesdays to Thursdays, found separately for US summer and winter on the bars the mask serves. A season with fewer than 20 such days is "n/a", not a failure; a symbol trading round the clock has no break to move.
- **Evidence:** `tests/test_report.py` (15 tests) and `tests/test_cli.py` (20); default suite 1225 passed (1 skipped without the mt5 extra); slow suite 7 passed (3 min 39 s, after the importer fix); ruff clean; `uv run desk --help` and a `data-report` of an empty folder run from the installed entry point. The whole suite catches all 12 mutants of T5's rules (weekend bars, more than 5 D1 bars, the break share, `passed` ignoring the break or our conversion, thin versus empty, the tick lag's sign, the weekend skip, and the importer fixes below: the refresh clamp, no partial days, partial days not masked, only one timeframe's first day), every file restored.
  - Years built from a New York schedule (not `SyntheticBarSource`, which aggregates on New York + 7 h and would partly assume the answer): a New York + 7 h year passes for GOLD and for FX, holidays and all; on a fixed GMT+2 clock GOLD's break moves from server 00h in winter to 23h in summer, and FX gets weekend bars and 6-bar weeks. A 10-minute shift fails our conversion; GMT+2 passes it and fails only XM's clock, so the two checks catch different faults.
  - `smoke` fails a failed `initialize`, a weekday tick 19 h old (the terminal T0 met) and a clock 5 min behind the ticks; at the server's weekend it skips the tick check and says so; a fake whose symbol info and tick carry distinctive bids and asks shows that no price is printed.
  - `import`, `refresh` and `data-report` run end to end on `FakeMt5` and exit 0, and the cache they leave reads back through `MarketData` as the source after the mask, every timeframe and column (Checkpoint B).
  - A cache reaching into 2025: `import`, `data-report` and a 2025 `smoke` print none of the more than 1,000 distinct 2025 prices.
  - With MetaTrader5 made unimportable, every command runs on a fake.
- **Deviations, for review:**
  - **"5 D1 bars per full week" became "no server week with more than 5"** (plan amended): a holiday leaves a 4-bar week, so "exactly 5" would fail every real year; a shifted clock shows as a sixth, Sunday bar.
  - **On a fixed GMT+2 clock, FX shows the weekend bars and GOLD shows the break.** GOLD reopens at 18:00 New York, which is server Monday 00:00 on GMT+2 even in summer; FX opens at 17:00 New York, server Sunday 23:00. The report runs both checks on every symbol.
  - **Holidays:** a New York holiday leaves the server day a few hours of bars (Christmas: about 6), so it shows as a thin day, as does an import's first server day (below); a weekday with no M15 bars at all shows as empty.
  - **The report's logic lives in `market/report.py`**, not `cli.py` (the plan computes the XM checks inside `market/`); `cli.py` only prints. `smoke`'s day of bars is read by `market/mt5.py`'s `recent_bars`: the Holdout guard caught the first draft reading bars in `cli.py`.
  - **The tick check takes the symbol with the freshest tick** (FX ticks through GOLD's daily break), and a tick ahead of this computer's clock fails too.
  - **`refresh` has no `--timeframes`:** a run that judges days must import every timeframe (T4).
- **An importer bug found by T5's end-to-end run, fixed (touches T4's `importer.py`, `cache.py` and `quality.py`):** after an import starting 1 January 2024, the first refresh failed with "GOLD M15 2023 would change bars on 2024-01-01". Server 1 January begins at 22:00 UTC on 31 December, but each timeframe's first chunk begins at UTC midnight, so the import held only part of that day (its D1 and H4 bars, which open at 22:00 UTC, not at all) and still judged and served it. The refresh read the 2023 chunk, found the evening's bars, and the served-bars guard refused them; without the refresh, the hole would have been served for good.
  - **The first import now masks the server day each timeframe's first chunk begins in**, whatever its count (plan amended, "Data-quality mask"). With the default start that is 1 January 2014, a holiday. Test: `test_the_server_day_an_import_begins_in_is_masked` (an import from 1 February 2024 masks server 1 January, where M15, H1, H4 and D1 begin, and server 1 February, where M1 begins; `MarketData` serves neither on any timeframe, and a refresh goes through).
  - **A refresh starts no earlier than the first chunk the cache holds** (`cache.first_cached`), so it doesn't fetch a year of history before the import's start. Test: `test_a_refresh_never_reaches_before_the_first_imported_chunk`.
  - **For the owner, not changed:** an import's `--start` picks M1's first month but the other timeframes' first year. So `--start 2024-02-01` serves January 2024 on M15 without M1 bars (M1 only settles same-bar conflicts), and a later import from an earlier date is refused, because it would add M1 bars to served January days. The default start, 1 January, has neither. If wanted, `desk import` could take a year instead of a date.

---

## Checkpoint B (after T4–T5)
- [x] A full import and refresh from `FakeMt5`, read back through `MarketData`, is green; ruff clean
- [x] The owner reviews (approved 2026-10-23)

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


---

## Tuning pass and independent review (2026-10-23, owner: "tune until perfect")

**Review fixes and the team's work, all merged earlier:** T4 (`89ac0e4`); price checks in `Bars`, symbol-checked cost paths, a positive-folds test (`39ac2c4`); the project `CLAUDE.md` (`f89fe7f`); `Bars.between` returns views, one symbol list in `market/universe.py`, no lists on sizing (`ad223a3`); one home for the server calendar and `sessions` moved to `market` (`d8d3ac2`); one `server_bound` (`a85af68`); tests for four unprotected rules (`df00c31`, `5912e18`).

**Checks on master `5912e18`:** the slow suite passed (7, 4 min 12 s); the whole-suite mutation run caught all 19 mutants (7 had survived the first review).

**An independent review (a fresh agent, read-only, with experiments) then found, and this pass fixed (uncommitted, for review):**
1. **Critical: a stale CANDIDATE could spend the Holdout.** The gate only looked at the same Hypothesis's latest Trial, but N is family-wide: a sibling Hypothesis's grid raised N from 1 to 1,105 and the CANDIDATE's Deflated Sharpe fell from 0.998 to 0.397, yet the request was accepted. Now `request_holdout` and `run_holdout` re-judge the stored daily R's Deflated Sharpe at the family's N today and refuse with `DEFLATED_SHARPE_FELL` (spec §6.3 gate table). Test: `test_a_sibling_grid_can_turn_a_candidate_stale_before_the_holdout`, failing without the gate.
2. **Required: today's swap credit was paid on every night back to 2014.** On a no-edge short-only random walk it moved the profit factor from 0.96 to 1.05. The Trial's `CostSnapshot` now counts a swap the position would earn as zero; the cost log keeps the measured rates (ADR 0004 amended).
3. **Required: judging ran ahead of the history.** A reconnected terminal's live tick with history still stopping days earlier had Monday and Tuesday judged empty, then wedged every refresh. Now a run whose last M15 bar closed more than 2 h before the cutoff fails before judging, and the last M15 bar's own day stays open (plan, "Import and cache").
4. **Required: a weekday with no M15 bars was judged but never masked**, so its H1/D1/M1 bars were served unvetted. Every server weekday is now judged; an empty one is masked (plan, "Data-quality mask").
5. **Required: a judging run that left out a timeframe locked it out for good.** A run that imports M15 must now import every timeframe.
6. **Required: a one-line order call slipped past the guards** (`getattr(api._module, "order" "_send")`). The scan now flags MT5 function names as strings (the parser joins split halves) and any `_module` reach outside `mt5_api.py`; the docstrings no longer claim the wrapper is airtight.
7. **Consider: a Windows rename onto a held cache file died with a raw `PermissionError`.** Retried briefly, then a `CacheError` naming the file.
8. **Consider: re-running an unchanged Spec re-rolled the Coin-Flip Baseline for free.** The seed now comes from what the Trial judges (its Combinations, bars, M1 bars and the rules version). This changes what a Trial computes, so **`RULES_VERSION` is 2** (note in `rules.py`); only the `trial` fingerprint moved, re-pinned with `PINNED_FOR = 2`.
9. **Consider: M1 was not fingerprinted.** Trial and Holdout records now carry `m1` (used, bar count, sha256); the Holdout settles on M1 when its Trial did. `RESULT_VERSION` is 3.
- **Nits:** the `UNIVERSE` alias is gone; `server_days` is imported from `server_time`.
- **Follow-on found while merging:** with empty weekdays now masked, a broker filling bars onto a holiday later would have wedged every refresh. The served-bars guard now compares only bars the cache serves, and names the earliest changed day.

**Final checks on the combined work (uncommitted):** ruff clean; the default suite 1188 passed (1 skipped without the mt5 extra); the slow suite 7 passed (4 min 2 s); the whole-suite mutation run caught all 25 mutants (the 19 above plus 6 for today's fixes), every file restored.

**Open owner decisions:** the Holdout counts a trade still open at data end at its last price (walk-forward training excludes such trades); `atr_target` receives a stop distance it ignores (trivial cost). Both kept as they are for now (owner, 2026-10-23).

- [x] The owner reviews (approved 2026-10-23)

---

## Simple app, built in one session (2026-10-23, owner: "forget the rigor rules, make it simple, done now")

**Owner's choices:** web app with AI chat; OmniRoute for the AI; real XM data now; no rules except no orders, commit as we go.

**Built:**
- `src/vibedesk/app/`: a FastAPI server and one page (`static/index.html`). Chat an Idea; the AI (OmniRoute, OpenAI-compatible) writes a Strategy Spec from the Block Library, with validation errors fed back up to 3 times; "Run test" runs it as a Trial on the real cache (walk-forward, blind years 2017-2024, real spreads and swaps) in about 10 s; the page shows the Verdict ("Looks real" or "No edge"), KPIs, cumulative R, net R per year and every check; each result goes back into the chat so "make it trade more" works; past tests are listed. Settings hold the OmniRoute URL, key (never sent back to the page) and model. "Update data" imports or refreshes every symbol from MT5 (read-only) and re-measures spreads.
- `Start Vibe Desk.bat` and a desktop shortcut "Vibe Desk": opens the SSH tunnel to OmniRoute (it listens only on the server), starts the app and opens the browser.
- The app extends a family's Trial Budget by itself and reuses the family that already holds an entry Block on a symbol.

**Real data (M3 T6 in effect):** all 8 symbols imported from the XM terminal (read-only), about 5 million bars each, judged through 2026-10-22; GOLD's first usable month is 2014-06, the FX pairs' 2014-01. Spread floors: GOLD 55 points, EURUSD 20, GBPUSD 24, USDJPY 25, AUDUSD 24, USDCAD 30, USDCHF 26, NZDUSD 30.

**Evidence:** `tests/test_app.py` (11 tests: the draft loop, the key never sent back, a Trial end to end on synthetic bars, the browser's 2.0-to-2 numbers); default suite 1237 passed; ruff clean. A click-through in the browser (canned AI, real bars, throwaway Ledger): chat, Spec card, Run test on real EURUSD H4 and GOLD H1, Verdict, charts, checks, history, phone width without sideways scrolling. The OmniRoute tunnel answers (401 until the key is set). Two bugs found and fixed: the browser wrote 2.0 back as 2 and the Spec refused [1.5, 2]; a test with 0 trades blamed the wrong check.

**First real results:** GOLD H1 Donchian breakout (20 or 55, ADX, ATR stop, 2R or 3R target): No edge, 1,184 blind trades, profit factor 0.80, 7 of 8 years losing. EURUSD H4 RSI bounce with ADX under 25, long only: never traded (no setting made 30 trades in its training years).

**Not verified:** the AI path against the real OmniRoute, which needs the owner's key.

**Issue fixed (2026-10-24, CCC-85): XM's clock follows European daylight saving (EET/EEST, Europe/Athens)**

- **Investigation & Confirmation from real cached bars:**
  - Real XM MT5 bars switch UTC offset on European DST dates (last Sundays of March and October at 01:00 UTC), not New York's (second Sunday of March, first Sunday of November).
  - The market trading week operates on New York time: FX opens Sunday 17:00 NY and closes Friday 17:00 NY; GOLD opens Sunday 18:00 NY with daily break 17:00–18:00 NY.
  - In the 3–4 mismatch weeks each year (between US switch and European switch):
    - 17:00 New York EDT is 21:00 UTC = 23:00 server time (EET, UTC+2).
    - FX opens Sunday 23:00 server time (4 M15 bars + 1 Sunday D1 bar).
    - GOLD opens Sunday 18:00 EDT = 22:00 UTC = Monday 00:00 server time (0 Sunday bars).
    - Friday closes at 17:00 EDT = 21:00 UTC = 23:00 server time (last M15 bar opens 22:45 server time).
    - Under the old code assuming New York + 7h all year, `server_to_utc` subtracted 3h instead of 2h during those weeks, skewing bar open times in UTC by 1 hour.
- **Fixes implemented:**
  - `src/vibedesk/market/server_time.py`: XM server clock switched to `Europe/Athens` (UTC+2 winter, UTC+3 summer). Added session clock utilities (`session_offset`, `session_weekdays`, `session_hours`) for New York session alignment.
  - `src/vibedesk/market/report.py`: `xm_clock_checks` now validates breaks against session hours (00h = 17:00 NY), verifies weekend bars against New York market hours, and permits the expected opening Sunday D1 bars in mismatch weeks without counting them as long weeks.
  - `src/vibedesk/market/synthetic.py`: trading schedule aligned with market week (`session_weekdays < 5`), with GOLD opening Monday server time.
  - Test suite updated: `test_server_time.py`, `test_report.py`, `test_mt5_source.py`, `test_fake_mt5.py`, `test_market_costs.py`, `test_synthetic.py`, `reference_engine.py`.
  - Full default suite (1,349 passed, 1 skipped) and ruff clean.
  - `RULES_VERSION` bumped to 3 in `src/vibedesk/rules.py` and `PINNED_FOR = 3` in `tests/test_rules_version.py`: server day and hour shifts in mismatch weeks alter `day_of_week` filters, swap nights, walk-forward/metrics daily boundaries and baseline hours on unchanged UTC bars, keeping old-clock Trials in the Ledger distinguishable.
- **Re-import results on all 8 symbols (read-only from XM MT5, ~41 million bars):**
  - **Before:** 104 out of 104 symbol-years failed clock checks (100% fail rate).
  - **After:** 103 out of 104 symbol-years passed all checks (99% pass rate).
  - Only GOLD 2014 flagged a known single-day US holiday anomaly (July 4, 2014, 2 weekend bars / 1 D1 bar).
  - Daily break on GOLD is 00h in both summer and winter across all years (2016–2026).
  - Re-measured Spread Floors and swaps with `desk floors` across all 8 symbols.

---

## M4 · CCC-43: Desk loop with `ScriptedLLM` (tool calls, SSE events, ≤ 12 tool calls per message)

**Description:** Spec §7.1 and §3.3. A Research Desk loop that takes an owner message, calls the LLM with tool definitions, executes the tool calls it returns, feeds results back, and repeats until the LLM answers in prose, capped at 12 tool calls per message. Streams Server-Sent Events (`message`, `tool_started`, `trial_progress`, `verdict`).

**Acceptance criteria:**
- [x] Desk loop executes tool calls, feeds results back into the conversation, and repeats until the LLM answers in prose, capped at `MAX_TOOL_CALLS = 12` per owner message (telling the owner plainly when the cap is hit).
- [x] Streams Server-Sent Events: `message`, `tool_started`, `trial_progress`, `verdict` (`Event.to_sse`, `sse_stream`, `parse_sse`, and FastAPI `POST /api/chat`; family chat route from spec §8 deferred until CCC-44 adds family tools).
- [x] Deterministic `ScriptedLLM` fake behind the same `complete(messages, tools)` / `chat(messages)` seam as `OmniRoute` (`src/vibedesk/app/llm.py`) drives all tests: normal run, the 12-call cap (single-call and multi-call turns), tool error recovery, and SSE event order.
- [x] Minimal `ToolRegistry` (`starter_tools`: `list_blocks`, `draft_spec`) ready for CCC-44's full tool set.

**Result (2026-10-24, commits `a982f5a`, `8201c5e`, and follow-up review fixes):**
- **Files:** `src/vibedesk/app/llm.py` (`ToolCall`, `Turn`, `ScriptedLLM`, `OmniRoute.complete`), `src/vibedesk/desk/loop.py` and `src/vibedesk/desk/__init__.py` (`MAX_TOOL_CALLS = 12`, `EVENT_TYPES`, `Event`, `Tool`, `ToolRegistry`, `starter_tools`, `desk_loop`, `DeskLoop`, `sse_stream`, `parse_sse`), `src/vibedesk/app/server.py` (SSE `POST /api/chat`; family chat route from spec §8 deferred until CCC-44), `tests/test_desk_loop.py` (12 tests).
- **Suites:** `uv run pytest` passes 1,361 tests (1 skipped without the mt5 extra); `uv run ruff check src tests` and `uv run ruff format --check src tests` clean.

---

## Issue CCC-90: OmniRoute 503 `resource_pressure` investigation and UI message (2026-10-24)

**Read-only server check (`ubuntu@54.254.218.196`):**
- **Host memory (`free -m`):** 3,832 MB total, 2,586 MB used, 541 MB free, 1,039 MB buff/cache, **1,246 MB available**; swap 1,302 / 2,047 MB used (745 MB free). Host `/proc/pressure/memory` is `avg10=0.00` (`avg60=0.05`, `avg300=0.09`).
- **Top processes by RSS:**
  - 2 `hermes` Python processes (PIDs 1265857 ~379 MB, 1267697 ~360 MB) + 1 `hermes` Node process (PID 1268082 ~102 MB) ≈ 841 MB total (down from 6+ processes / ~1.7 GB on 2026-10-23).
  - `omniroute (v16.3.5)` child server (PID 1032776, ~329 MB RSS, ~222 MB VmSwap) + supervisor (PID 595186, ~15 MB RSS); cgroup `memory.current` = 359.5 MiB (`376,963,072` bytes), cgroup `memory.pressure` = `avg10=0.00`.
  - `open_webui` uvicorn (PID 1038908, ~211 MB).
  - 2 `opencode` processes (PIDs 1037826 ~172 MB, 932379 ~154 MB) ≈ 326 MB.
  - `chromium` for `aipass` (~304 MB across 4 processes).
- **Live OmniRoute check via `llm.py`:** `POST /v1/chat/completions` (`OmniRoute.chat`) still fails with `503 resource_pressure` after all 4 tries (initial + 2 s, 5 s, 10 s retries), so a real chat end to end cannot complete until `omniroute.service` is reconfigured and restarted by the owner.

**Root cause (from `~/.omniroute/logs/application/app.log`, `~/.config/systemd/user/omniroute.service`, and `open-sse/utils/resourcePressurePolicy.ts`):**
1. **`cgroup_high` trips during startup warm-up:** `omniroute.service` sets `Environment=OMNIROUTE_MEMORY_MB=384`, `MemoryHigh=512M`, `MemoryMax=640M`. In `resourcePressurePolicy.ts`, `DEFAULT_RESOURCE_PRESSURE_THRESHOLDS` has `criticalRatio = 0.92`, `highRatio = 0.85`, `recoveryRatio = 0.75`. For `cgroup_high`, OmniRoute compares raw `memory.current` (including page cache) against `MemoryHigh` (`0.92 × 512 MiB = 471 MiB`). 72 s after PID 1032776 started (`2026-10-23T07:57:05Z`), `cgroupCurrentMb` hit `494 MiB` (`416 MiB` working set + `78 MiB` file cache) with `psiSomeAvg10=0.36`, tripping `reason=cgroup_high` into `critical`.
2. **Permanent latch in `critical` (`recoveryStreak=0`):** Once in `critical`, `isRecovered()` requires **all three** ratios (`v8.heapUsedBytes / v8.heapLimitBytes`, `cgroupWorkingSetBytes / cgroup.maxBytes`, `cgroup.currentBytes / cgroup.highBytes`) to drop to `≤ recoveryRatio` (`0.75`). Even though `cgroupCurrentMb` later fell to `361 MiB` (`361 / 512 = 0.705 ≤ 0.75`) and PSI dropped to `0.00`, `OMNIROUTE_MEMORY_MB=384` pins `--max-old-space-size=384` (`v8.heapLimitBytes ≈ 392 MiB`, so `0.75 × 392 MiB = 294 MiB`). OmniRoute's normal idle V8 heap is ~300–325 MiB (`heapUsed / heapLimit ≈ 0.78–0.83`), which is below `highRatio` (`0.85`) so it never causes pressure on its own, **but above `recoveryRatio` (`0.75`) so `isRecovered()` never returns `true`**. Because `OMNIROUTE_PRESSURE_SELF_RESTART` is also unset, the process stays wedged in `critical` (`503 resource_pressure`) until restarted, and re-wedges ~70 s after every restart under the current 384M/512M limits.

**Recommended commands for the owner (on `ubuntu@54.254.218.196`):**
1. Raise OmniRoute's V8 heap ceiling (`OMNIROUTE_MEMORY_MB=640`, so `0.75 × 640 = 480 MB` clears the ~315 MB idle heap with recovery headroom) and cgroup limits (`MemoryHigh=768M`, `MemoryMax=896M`), opt out of noisy-neighbor host PSI (`OMNIROUTE_PRESSURE_PSI_DISABLED=1`), and enable self-restart (`OMNIROUTE_PRESSURE_SELF_RESTART=1`):
   ```bash
   ssh -i "D:\millmaker\ssh-key.pem" ubuntu@54.254.218.196
   ```
   ```bash
   cat <<'EOF' > ~/.config/systemd/user/omniroute.service.d/override.conf
   [Service]
   Environment=NODE_OPTIONS=--max-semi-space-size=4
   Environment=OMNIROUTE_SERVER_HOST=127.0.0.1
   Environment=OMNIROUTE_MEMORY_MB=640
   Environment=OMNIROUTE_PRESSURE_SELF_RESTART=1
   Environment=OMNIROUTE_PRESSURE_PSI_DISABLED=1
   MemoryHigh=768M
   MemoryMax=896M
   EOF
   systemctl --user daemon-reload
   systemctl --user restart omniroute.service
   systemctl --user status omniroute.service
   ```
2. Optional host cleanup if extra RAM headroom is wanted (frees ~326 MB from idle `opencode` processes):
   ```bash
   kill 1037826 932379
   ```

**App change:**
- `src/vibedesk/app/llm.py`: when OmniRoute answers `resource_pressure` (or `heap_pressure`), raise a plain message (`"OmniRoute is stuck under memory pressure on the server ({status} resource_pressure{retried}). Restart omniroute.service with the raised limits (see tasks/todo.md CCC-90 or AGENTS.md), then try again."`) using `response.status_code` and only claiming a retry when the status code is in `BUSY`.
- `tests/test_app.py`: added `test_resource_pressure_after_retries_shows_a_plain_message_in_the_chat` and `test_resource_pressure_without_retry_reports_status_and_no_retry_claim`.


---

## M4 · CCC-45: Grounding Rule validator: no digits in prose, placeholders rendered from stored data

**Description:** Spec §7.3, §9.4, and ADR 0005. Grounding Rule validator and placeholder renderer for assistant prose:
- `validate(prose, *, known_tokens=None) -> list[str]`: Assistant prose may not contain raw digits. Digits are permitted only inside whitelisted known tokens (universe symbols, timeframes M1/M15/H1/H4/D1, Block names) and valid placeholders (`{{trial:ID.metric}}`, `{{spec:ID.param}}`, `{{hyp:ID.field}}`, `{{date:ISO}}`). Malformed placeholders or unknown placeholder kinds fail validation.
- `render(prose, lookup) -> str`: Pure placeholder renderer replacing each placeholder with `lookup(kind, id, field)` (for date: `kind="date"`, `id=ISO`, `field=""`). Unresolved placeholders (where `lookup` returns `None`), unknown placeholder kinds, and malformed placeholders raise `GroundingError`.
- `PLACEHOLDER_PATTERN`, `PLACEHOLDER_REGEX`, `KNOWN_PLACEHOLDER_KINDS` exported for consumers. Pure implementation with no I/O and no Ledger access.

**Acceptance criteria:**
- [x] Raw digits in prose rejected; digits glued to words ("20x", "1.5R", "M150") rejected.
- [x] Whitelisted tokens pass validation: universe symbols (GOLD, EURUSD, ...), timeframes (M1, M15, H1, H4, D1), and Block names (donchian_breakout, ...), including custom tokens passed via `known_tokens`.
- [x] Valid placeholders pass validation with numbers allowed inside.
- [x] Unknown placeholder kinds and malformed placeholders fail validation and render.
- [x] `render(prose, lookup)` replaces placeholders from fake lookup; unresolved placeholders (`lookup -> None`) raise `GroundingError`.
- [x] Date placeholder passes `("date", ISO, "")` to lookup.
- [x] Exported regex grammar and small constants for other modules.
- [x] Full test suite green (`uv run pytest`, `uv run ruff check src tests`, `uv run ruff format --check src tests`).

**Result (2026-10-28):**
- **Files:** `src/vibedesk/desk/grounding.py` (new), `tests/test_grounding.py` (new).
- **Suites:** 20 dedicated tests in `tests/test_grounding.py` passing; full test suite (1,383 passed, 1 skipped) green. Clean ruff check and format. Strict ISO date validation and whitespace rejection addressing review comments.

---

## Wave 0: Contract for Layer A parallel teams (2026-10-28)

**Description:** Contract worker providing the shared code-level interfaces, types, stubs, and file ownership definitions for Wave 1 and Wave 2 parallel teams.

**Acceptance criteria:**
- [x] Code-level interfaces implemented with types, docstrings, and stubs:
  - `vibedesk.notify`: `Notice`, `NoticeKind`, `Notifier`, `RecordingNotifier` (full), `TelegramNotifier` (stub), `get_notifier`.
  - `vibedesk.desk.budget`: `TokenBudget`, daily reset at 00:00 Bangkok time (UTC+7), `BudgetState` (ok, warn, paused).
  - `vibedesk.desk.grounding`: interface documented for Desk team; module owned in full by separate CCC-45 worker.
  - `vibedesk.desk.tools`: `DeskContext`, `build_desk_tools(ctx)` registering all 7 tools from spec §7.2.
  - `vibedesk.app.scheduler`: `Scheduler` with the 3 maintenance jobs (nightly refresh, weekly floors, daily digest).
  - `vibedesk.app.auth`: `OWNER_COOKIE_NAME` (`vibedesk_owner`, SameSite=Strict), `require_owner` FastAPI dependency.
  - `vibedesk.app.api_spec`: `ENDPOINT_TABLE` (15 endpoints), `MCP_TOOL_MAP` (8 tools).
  - `vibedesk.app.settings`: Settings keys and git-ignored `data/secrets.json` storage.
- [x] Plan and ownership: `tasks/plan.md` updated with Layer A teams section, File Ownership Table, merge order, and acceptance checklists.
- [x] Test suite: `tests/test_contract.py` (10 tests) green, `test_guard.py` clean (no forbidden order calls), ruff clean.

---

## Wave 1: Desk Team (M4)

### CCC-44: Desk tools (list_blocks, describe_data, draft_spec, register_hypothesis, run_trial, get_trial, request_holdout)
- [x] Implement `describe_data` for Research Window stats (coverage, Spread Floor, ATR levels).
- [x] Implement `register_hypothesis` to freeze Hypothesis in Ledger with anti-gaming checks.
- [x] Implement `run_trial` executing Gauntlet, counting 1 Trial against family budget, raising BudgetExhausted when spent.
- [x] Implement `get_trial` retrieving stored result and metrics.
- [x] Implement `request_holdout` checking §6.3 gate (CANDIDATE, Deflated Sharpe, trades >= 30).
- [x] Verification: `uv run pytest tests/test_desk_tools.py` (9 passed).

### CCC-45: Grounding Rule (wiring into desk loop)
- [x] Interface called by Desk team: `validate(prose, *, known_tokens=None) -> list[str]` and `render(prose, lookup) -> str` (resolving `{{trial:ID.metric}}`, `{{spec:ID.param}}`, `{{hyp:ID.field}}`, `{{date:ISO}}`).
- [x] Built and tested by separate CCC-45 worker in `src/vibedesk/desk/grounding.py` and `tests/test_grounding.py` (PR #19).
- [x] Wires into `desk/loop.py`: validates assistant prose, retries up to 2 times with a correction message, renders placeholders from stored data, or withholds explanation and sends verdict alone if retries fail. Re-exported in `src/vibedesk/desk/__init__.py`.
- [x] Verification: `uv run pytest tests/test_grounding.py tests/test_desk_loop.py` (32 passed).

### CCC-46: Token budget enforcement
- [x] Count tokens from OmniRoute usage responses against daily cap.
- [x] Send `token_warning` notice to Notifier at 80% of daily cap (once per day).
- [x] Pause desk loop at 100% of daily cap with clear owner explanation while manual Trials remain permitted.
- [x] Daily reset at 00:00 Asia/Bangkok time (UTC+7).
- [x] Verification: `uv run pytest tests/test_budget.py` (9 passed).

**Result (2026-10-28):**
- **Files:** `src/vibedesk/desk/tools.py`, `src/vibedesk/desk/loop.py`, `src/vibedesk/desk/__init__.py`, `tests/test_desk_tools.py` (new), `tests/test_budget.py` (new), `tests/test_desk_loop.py`, `tests/test_contract.py`.
- **Done-When (M4, spec §10):** A scripted Idea reaches a Verdict end to end with ScriptedLLM. All 7 desk tools operational, Grounding Rule retries and placeholder rendering active, TokenBudget daily cap and 80% warning enforced.
- **Review fixes addressed:**
  - Context-free tool registration: `starter_tools()` and `build_desk_tools()` expose only `list_blocks` and `draft_spec` when unconfigured (`ledger`/`market` is None), preventing live `/api/chat` model call failures.
  - Honest Verdict cost refusal: `run_trial` refuses with clear ValueError when no Spread Floor is measured instead of falling back to 0 spread floor and hard-coded point.
  - Compact tool returns: `run_trial` and `get_trial` return compact summaries with headline metrics and checks, bounded under 4KB, keeping full trade/daily arrays in Ledger.
  - Mid-message budget pause: turns crossing 100% with prose answers finish normally (grounding check, history updated to `_sink`) before reporting pause.
  - Compact check keys: `_compact_trial_summary` extracts `name`, `status`, `value`, `threshold`, and `at_most` matching `Check.to_dict()`.
  - Ambiguous spec placeholders: bare parameter names matching multiple blocks (e.g. entry and filter `period`) return None to keep placeholders unresolved and trigger grounding retries; dropped dead dotted-path branch; rounded all float lookup values consistently.
- **Suites:** 45 desk/budget tests passed. Full suite: 1,416 passed, 1 skipped. Clean ruff check and format.

---

## Wave 1: API Team (M5 Backend)

### CCC-50: HTTP API with owner-only actions
- [ ] Implement 15 endpoints in `ENDPOINT_TABLE`.
- [ ] Enforce `require_owner` on `approve_holdout`, `extend_budget`, `PUT /api/settings` (HTTP 403 on missing cookie).
- [ ] Wire `POST /api/families/{id}/chat` to SSE stream running `desk_loop` with `build_desk_tools`.
- [ ] Verification: `uv run pytest tests/test_api.py`

### CCC-53: Scheduler
- [ ] Background thread for nightly refresh (02:00 Bangkok), weekly floors (Sunday 00:00 Bangkok), daily digest (21:00 Bangkok).
- [ ] Manual trigger `run_job_now(name)`.
- [ ] Verification: `uv run pytest tests/test_scheduler.py`

### CCC-55: Holdout and Trial carry-forwards
- [ ] Reusable Holdout grant until spend is recorded.
- [ ] Bind Holdout Request to Trial ID.
- [ ] Per-family trial lock to freeze N during execution.
- [ ] Verification: `uv run pytest tests/test_carry_forwards.py`

---

## Wave 1: Notify Team (M6 Part)

### CCC-59: Telegram notices and daily digest
- [ ] Implement `TelegramNotifier.send` over httpx.
- [ ] Format and push CANDIDATE, PASS, HOLDOUT FAIL, Holdout Request, and token warning notices.
- [ ] Compile and push 21:00 Bangkok daily digest.
- [ ] `scripts/setup-telegram.ps1` for interactive setup into `data/secrets.json`.
- [ ] Verification: `uv run pytest tests/test_notify.py`

---

## Wave 2: UI Team (M5 Frontend)

### CCC-52 & CCC-56: 4-page UI and Playwright E2E
- [ ] Build Desk, Ideas, Ledger, and Settings in `src/vibedesk/app/static/index.html`.
- [ ] Set `vibedesk_owner` cookie with SameSite=Strict on page load.
- [ ] Responsive layout at 375 px phone width without horizontal scroll.
- [ ] Playwright E2E suite on deterministic fakes.
- [ ] Verification: Playwright test suite

---

## Wave 2: MCP Team (M6 Part)

### CCC-58 & CCC-60: Stdio MCP Server and Refusal Tests
- [ ] Build stdio MCP server calling HTTP API on `127.0.0.1:8765`.
- [ ] Expose 5 read tools and 3 act tools per spec §8.2.
- [ ] Omit all owner tools (no approve, no budget, no settings).
- [ ] Automated tests asserting 403 refusals and RecordingNotifier capture.
- [ ] Verification: `uv run pytest tests/test_mcp.py`

---

## Wave 3: Whole-App Team (M7)

### CCC-61, CCC-62, CCC-63, CCC-64: Whole-app E2E and Click-through
- [ ] Whole-app E2E on deterministic fakes.
- [ ] Browser click-through with playwright-cli checking console errors and phone width.
- [ ] Document commands in `CLAUDE.md`.
