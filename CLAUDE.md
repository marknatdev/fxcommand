# Vibe Desk

An AI research desk for one owner who trades GOLD and FX on MT5 (XM). The owner chats an Idea, the Research Desk builds a block-based Strategy Spec, and the Gauntlet gives an honest Verdict. Deterministic code executes; the AI never trades.

- **Intent:** `docs/intent/vibe-desk.md`. **Spec:** `docs/spec/layer-a-verdict-engine.md`. **Glossary:** `CONTEXT.md` (use its terms exactly). **Decisions:** `docs/adr/`.
- **Current work:** `tasks/plan.md` (definitions the tests depend on) and `tasks/todo.md` (tasks, results, checkpoints). Finished milestones: `tasks/done/`.

## Commands

```bash
uv sync                                   # dependencies (add --extra mt5 for the live terminal)
uv run pytest                             # default suite, 4 workers (~30 s)
uv run pytest -m slow                     # known answers, benchmarks, 12-year import (~4 min)
uv run pytest -n 0 tests/test_x.py        # one file, one process
uv run ruff check src tests
uv run ruff format --check src tests
uv run python tests/test_rules_version.py # print the rules fingerprints (re-pin)
uv run desk                               # read-only market commands (M3 T5)
```

## Layout

- `src/vibedesk/blocks/`: the Block Library, Strategy Specs, compiling a Combination into Signals.
- `src/vibedesk/engine/`: the numba fill engine, costs and swap, metrics in R.
- `src/vibedesk/gauntlet/`: walk-forward, the Coin-Flip Baseline, statistics, the Prosecutor, the Pass Bar, Trials and the Holdout.
- `src/vibedesk/ledger/`: the append-only SQLite Ledger.
- `src/vibedesk/market/`: bars, the server clock, the Holdout guard (`data.py`), the MT5 seam (`mt5_api.py`, `fake_mt5.py`, `mt5.py`), the Parquet cache and mask, Spread Floors, the importer.
- `tests/`: `reference_engine.py` is an independent twin of the engine; `test_rules_version.py` fingerprints the trading rules.

## Rules that are never bent

- **No orders, ever.** Nothing in `src/` may name an order or position call; MT5 is reached only through `real_api()`, which wraps it in `ReadOnlyMt5`. Tests use `FakeMt5`. Only the owner starts anything live.
- **The Holdout (2025-01-01 on) is locked.** Bars come only through `MarketData`, which refuses the Holdout without a Ledger-approved grant. Reports show only structural facts (counts, clock checks, spreads, gaps) for Holdout years, never prices or returns.
- **Every evaluation is a Trial** (ADR 0003). No uncounted look at real results: a run on real data that isn't recorded in the Ledger prints timing and sanity only.
- **The Ledger is append-only.** No UPDATE or DELETE outside migrations; a schema change is a new migration.
- **Trading rules are versioned.** A change that can alter an existing Spec's trades or Verdict bumps `RULES_VERSION` in `src/vibedesk/rules.py` with a note, then re-pins `tests/test_rules_version.py`. A refactor must pass that test with its pins unchanged; never re-pin to make a refactor green.
- **Served bars never change.** The importer refuses a chunk that would change any bar the cache serves (a judged day the mask lets through). It never judges ahead of the history: a run whose M15 history lags the last tick by more than 2 h fails, and a run that imports M15 imports every timeframe.

## Working here

- The owner approves each checkpoint and each commit; commit messages end with the `Co-Authored-By` line. Don't push.
- Plans and results go in `tasks/`; the owner reviews at each checkpoint.
- Parallel work runs in worktrees with explicit file ownership; the shared contract is committed first.
- Times are UTC epoch seconds; the XM server clock is New York + 7 h (`market/server_time.py`).
