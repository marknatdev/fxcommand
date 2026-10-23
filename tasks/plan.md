# Implementation Plan: Layer A, Milestone M3 (Market data from MT5, read-only)

## Overview

M3 replaces synthetic bars with XM's real history. A read-only importer copies bars from the MT5 terminal into a Parquet cache in UTC, masks the unusable stretches, and measures each symbol's Spread Floor and swap. The `market` facade then serves real bars to the Gauntlet exactly as it served synthetic ones, with the Holdout guard unchanged.

M3 adds no LLM (that is M4) and no app (M5). Its commands run from a `desk` CLI.

**Done when (spec §10):**
- a read-only import fills the cache;
- the UTC conversion is checked against `symbol_info_tick` times;
- Spread Floors are measured;
- `desk smoke` passes.

Detailed tasks: [todo.md](todo.md). Finished milestones: [done/](done/).

## Safety: read-only, by construction

- **The MT5 calls M3 may use** (spec §3.3, plus `symbol_info_tick`, which §10 needs, and `last_error` for diagnostics): `initialize`, `shutdown`, `last_error`, `symbol_info`, `symbol_info_tick`, `symbol_select`, `copy_rates_range`, `copy_ticks_range`, and the `TIMEFRAME_*` and `COPY_TICKS_*` constants. `symbol_select` only adds a symbol to the terminal's Market Watch, so that its history can be read.
- **Guard tests:**
  - The existing scan for `order_send`, `order_check`, `positions_*` and `orders_*` in `src/` stays.
  - New: an allowlist test parses `src/` and fails on any MetaTrader5 attribute outside the list above.
  - New: only `market/mt5.py` may import `MetaTrader5`.
- **Agents and tests:** tests run on `FakeMt5` only. Agents may run the read-only commands (`desk smoke`, `desk import`, `desk floors`) against the real terminal; nothing in M3 can place, change or close an order.

## Definitions the tests depend on

These are fixed before code, so no test is tuned after the fact.

### Server clock to UTC (spec §2, §4)
- **MT5's numbers:** epoch seconds that MT5 returns are server wall-clock time written as if it were UTC. The XM server runs New York time + 7 h.
- **Conversion:** `utc = server − offset`, where `offset` is New York's UTC offset + 7 h at the New York wall time `server − 7 h`. This is the inverse of the existing `server_offset(utc)`.
- **No ambiguity for trading bars:** New York changes its clocks at 02:00 on a Sunday, which is 09:00 server time, while the market is closed.
- **Requests:** `copy_rates_range` and `copy_ticks_range` take server-clock bounds. A UTC range is widened by one day on each side, converted, and trimmed back to the UTC range after conversion.
- **Checks:**
  - **Round trip:** utc → server → utc is the identity for every minute of 2014–2026, including every DST weekend.
  - **Structure, on real data, per year:** every D1 bar opens at 17:00 New York; every H4 bar opens at a server hour divisible by 4; every M15 bar sits on the 15-minute UTC grid. A year that breaks this means XM's clock differed then, and the import report fails it.
  - **Live tick (`desk smoke`):** while the market is open, the converted `symbol_info_tick` time is within 120 s of the system's UTC clock. At the weekend this check is skipped with a message, and the structural checks still run.

### Import and cache (spec §4)
- **Universe:** GOLD, EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF, NZDUSD. Timeframes M15, H1, H4 and D1, plus M1 for settling same-bar conflicts.
- **Range:** from `RESEARCH_START` (2014-01-01) to now. Holdout years are imported too: a Holdout run needs them, and only the `MarketData` facade, behind the grant, can read them.
- **Closed bars only:** each import fixes a cutoff time when it starts. Only bars that have closed by then (open time + timeframe ≤ cutoff) are cached. The bar still forming is never cached, so no partial bar is kept for good.
- **Chunks:** M1 by calendar month, the others by calendar year. MT5 may still be downloading history when first asked, so a chunk is accepted only when two reads in a row return the same closed bars (count and last time). Comparing closed bars only means a new bar arriving between reads does not stop the current chunk from settling.
- **Layout:**
  - `data/bars/{symbol}/{timeframe}/{year}.parquet`: UTC `time` (int64), `open`, `high`, `low`, `close` (float64) and `spread` (int32).
  - `data/bars/{symbol}/meta.json`: point, digits, the first usable month, the masked days, the last import time.
- **Writes:** every file is written to a temporary name and renamed into place, so a crash never leaves half a year. One importer at a time, enforced by a lock file. (ADR 0006 makes the app the single writer; until M5 builds the app, the CLI writes, and M5 moves refresh into the app.)
- **Refresh:** incremental. The current chunk and the one before it are re-imported, which covers a year or month that ended since the last refresh. Older chunks are never touched again.

### Data-quality mask (spec §4)
- **First usable month:** the first calendar month in which the median count of M15 bars per server day reaches 50% of the symbol's median over the Research Window. Bars before it are excluded. GOLD is expected around mid-2014.
- **Thin days:** a server day with fewer than 50% of that median M15 count is excluded from every timeframe of the symbol.
- **Frozen at first import:** the median comes from the Research Window only (2014–2024), and the mask is stored in `meta.json`. A refresh adds new days to the mask but never re-judges old ones, so a Trial's bars, and their fingerprint in the Ledger, stay the same.
- **Where it applies:** `CachedBarSource` applies the mask on every read, so Trials and Holdouts see the same bars.

### Spread Floor and swap (spec §4, ADR 0004)
- **Spread Floor:** the median of `(ask − bid) / point` over the last 14 days of ticks (`copy_ticks_range` with `COPY_TICKS_INFO`), counting only ticks with both a bid and an ask. The 90th percentile and the tick count are stored with it. Only the spread is kept; the price levels of those ticks (Holdout-period data) are never stored or shown.
- **Swap:** `swap_long` and `swap_short` from `symbol_info`. The importer refuses a symbol whose `swap_mode` is not 1 (points per lot), and takes the triple-swap day from `swap_rollover3days`.
- **Store:** each measurement is appended to `data/costs/{symbol}.jsonl`. `current_cost(symbol)` returns the latest as a `CostSnapshot`, with 1 point of slippage and `as_of` set to the measurement date.
- **Weekly re-measurement:** `desk floors` runs it in M3; M5 schedules it.

### The `desk` CLI
- **`desk smoke`** (spec §9.7, run by hand): connect, read `symbol_info` for every symbol, check the live tick's UTC time, read the last day of GOLD M15 and one hour of ticks, disconnect. It prints a report and exits non-zero on any failure. It prints counts and times, never prices.
- **`desk import`**, **`desk refresh`**, **`desk floors`**: as defined above.
- **`desk data-report`:** per symbol, the first usable month, masked days per year, the structural clock checks per year, the median bar spread against the floor, and the gaps.

### Reports stop at the Holdout
Every report (the import report, `desk data-report`, `desk smoke`) is computed inside `market/`. For years from `HOLDOUT_START` on, it shows only structural facts: bar counts, clock checks, spreads and gaps. It never shows prices, returns or anything derived from them. Otherwise a report would be a side door into the Holdout (ADR 0002).

## Architecture decisions

- **One seam, two adapters (spec §3.3):** an `Mt5Api` Protocol in `market/mt5_api.py` lists exactly the allowed calls. The real adapter is the `MetaTrader5` module itself. The fake, `FakeMt5`, serves `SyntheticBarSource` data in server-clock time, with ticks and `symbol_info`. Everything above the seam is tested on the fake.
- **`Mt5BarSource(api)`:** a `BarSource` over the live terminal, converting to UTC. Only the importer uses it.
- **`CachedBarSource(root)`:** a `BarSource` over the Parquet cache, which applies the mask. This is what `MarketData` uses for real Trials. The existing guard (only `market` calls `.fetch`) now covers the cache too, plus a new check that only `market/cache.py` reads Parquet.
- **Dependencies:** `pyarrow` for the cache, and `MetaTrader5` (Windows only) as an optional `mt5` extra, so the tests never need a terminal.

## Task list

### Phase 0: the contract (here, before any parallel work)
- [ ] T0: `Mt5Api` Protocol, `FakeMt5`, dependencies, the `desk` entry point, the MT5 allowlist guard

### Phase 1: three tasks that own separate files and can run in parallel
- [ ] T1: Server clock to UTC + `Mt5BarSource`
- [ ] T2: Parquet cache + data-quality mask + `CachedBarSource`
- [ ] T3: Spread Floor + swap into a `CostSnapshot`

### Checkpoint A
- [ ] Default suite green on the fakes, ruff clean; the owner reviews

### Phase 2: integration (needs Phase 1)
- [ ] T4: Importer and refresh
- [ ] T5: `desk` commands: smoke, import, refresh, floors, data-report

### Checkpoint B
- [ ] A full import and refresh from `FakeMt5` into a temporary cache, read back through `MarketData`, is green; the owner reviews

### Phase 3: real data (here, read-only, against the XM demo terminal)
- [ ] T6: Real import of all 8 symbols, the data report, the Spread Floors, `desk smoke`
- [ ] T7: A timing and sanity run on real data, which records no result (see the open questions)

### Checkpoint C: M3 done
- [ ] The spec §10 M3 items are met; `uv run pytest` and `uv run pytest -m slow` are green; the owner reviews

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| XM's server clock was set differently in early years | High: every early session filter and swap night would be off | Structural clock checks per year in the data report (T6); a failing year blocks the checkpoint |
| MT5 returns partial history while it downloads | High: silently short folds | Two matching reads per chunk (T4); bar counts per day checked against the frozen median (T2, T6) |
| The terminal's "Max bars in chart" setting caps a read | Medium | Monthly M1 chunks; the report flags a chunk that returns exactly the cap |
| Memory: M1 for 8 symbols × 12 years is about 30 million rows | Medium (about 4 GB free) | Import chunk by chunk, write per year, never hold a symbol's whole M1 |
| Real data breaks an engine assumption (zero-range bars, zero spread, long gaps) | Medium | T7 runs the Gauntlet on real GOLD M15 and checks for NaN, empty folds and timing |
| Holdout data sits in the cache | High if bypassed | Only `MarketData` reads the cache; guard tests (Parquet reads only in `market/cache.py`); reports show only structural facts for Holdout years |
| A forming bar or a year rollover leaves a partial bar in the cache | High: wrong bars kept for good | Closed bars only, by a cutoff fixed at the start; refresh re-imports the previous chunk too (T4) |
| A look at real results that the Ledger doesn't count | High: the anti-reset rule (ADR 0003) leaks | T7 records timing and sanity only, never a Verdict, profit factor or net R |
| GOLD's history starts mid-2014 | Low | The first fold trains on about 2.5 years; the report records the first usable month |
| A floor measured over a quiet or a wild fortnight | Low | The p90 and tick count are stored beside the median; ADR 0004's weekly re-measure |

## Owner decisions (2026-10-23)

- **The first real-data run (T7): timing and sanity only.** A result on real data that the Ledger doesn't count is exactly the uncounted look ADR 0003 exists to stop. So the Gauntlet runs on real GOLD M15 in a throwaway Ledger, and only the time and the sanity checks (no NaN, every fold has bars, baseline counts match) are recorded. The Verdict, profit factor and net R are never printed or written down. The real `data/desk.db` starts empty.
- **Re-runs after a rule change are charged** like any Trial, as built (ADR 0003 unchanged).
- **Phase 1 runs as three parallel worktree sessions** (T1, T2, T3), started after T0 is committed. Each owns only its listed files; merging waits for the owner.

## Carried forward

- **For M5 (the API and app):** the grant is reusable until the spend is recorded; bind each Holdout Request to its Trial id; concurrent Trials can share a seed; schedule the nightly refresh and the weekly floors.
- **For Layer B (the live runner):** it always sits on the latest bar, where a time filter uses the clock's next open; it must re-check time filters against the bar it actually fills on.
