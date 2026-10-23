# Vibe Desk

An AI research desk for one owner who trades GOLD and FX on MT5 (XM). The owner chats an Idea, the Research Desk builds a block-based Strategy Spec, and the Gauntlet gives an honest Verdict. Deterministic code executes; the AI never trades.

- **Intent:** `docs/intent/vibe-desk.md`. **Spec:** `docs/spec/layer-a-verdict-engine.md`. **Glossary:** `CONTEXT.md` (use its terms exactly). **Decisions:** `docs/adr/`.
- **Current work:** `tasks/plan.md` (definitions the tests depend on) and `tasks/todo.md` (tasks, results, checkpoints). Finished milestones: `tasks/done/`.

## Commands

**Start the app:** double-click `Start Vibe Desk.bat` (or the desktop shortcut "Vibe Desk"). It opens the SSH tunnel to OmniRoute on the Ubuntu server, starts the server on http://127.0.0.1:8765 and opens the browser. The OmniRoute key goes in the app's Settings (saved to `data/settings.json`) or in `.env` (see `.env.example`). "Update data" imports or refreshes every symbol from MT5 (read-only) and re-measures spreads.

```bash
uv run --extra mt5 vibedesk-web --open    # the app, by hand
uv run python tests/demo_server.py        # the app on port 8766 with a canned AI and a throwaway Ledger
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

- `src/vibedesk/app/`: the web app: `server.py` (FastAPI and the API), `static/index.html` (the one page), `research.py` (Idea to Spec through the AI), `llm.py` (OmniRoute client), `desk.py` (Trials, history, data updates), `settings.py`.
- `src/vibedesk/blocks/`: the Block Library, Strategy Specs, compiling a Combination into Signals.
- `src/vibedesk/engine/`: the numba fill engine, costs and swap, metrics in R.
- `src/vibedesk/gauntlet/`: walk-forward, the Coin-Flip Baseline, statistics, the Prosecutor, the Pass Bar, Trials and the Holdout.
- `src/vibedesk/ledger/`: the append-only SQLite Ledger.
- `src/vibedesk/market/`: bars, the server clock, the Holdout guard (`data.py`), the MT5 seam (`mt5_api.py`, `fake_mt5.py`, `mt5.py`), the Parquet cache and mask, Spread Floors, the importer.
- `tests/`: `reference_engine.py` is an independent twin of the engine; `test_rules_version.py` fingerprints the trading rules.

## Rules

The owner dropped the rigor rules on 2026-10-23: keep it simple and ship. One stays:

- **No orders, ever.** Nothing in `src/` may name an order or position call; MT5 is reached only through `real_api()`, which wraps it in `ReadOnlyMt5`. Tests use `FakeMt5`. Only the owner starts anything live.

The machinery behind the old rules still runs and the existing tests keep it working, but it is no longer a gate on new work: the app runs every test as a Trial in the Ledger and extends a family's Trial Budget by itself; the Holdout (2025 on) stays out of tests until someone builds a button for it; `RULES_VERSION` and its pins, the served-bars guard and the clock checks are kept but not extended. No per-task checkpoints, mutation runs or independent reviews unless the owner asks.

## Working here

- Commit as you go, without asking; commit messages end with the `Co-Authored-By` line. Don't push.
- Plans and results go in `tasks/`.
- **Keep the Vibe Kanban board in step (owner's rule).** Whenever a task, checkpoint, decision or issue starts, finishes or changes, or a new one is planned, update the Fxcommand board through the `vibe_kanban` MCP in the same turn, after `tasks/` is updated:
  - **Where:** project "Fxcommand" (`0f530a08-1bad-4dbd-ad53-b2452e8c7e07`). Find an issue by its title prefix: `Roadmap · Layer A · M4 …` for epics, `M3 · T5 …` for tasks, `Issue · …`, `Decision · …`, `Chore · …`.
  - **What:** set its status (Backlog, To do, In progress, In review, Done, Cancelled); tick its acceptance checklist in the description and add the result commit; add new tasks as sub-issues of their milestone epic with the same title style and tags (feature, enhancement, documentation, bug); link blockers with a `blocking` relationship.
  - `tasks/` stays the source of truth; the board follows it. If the MCP is unreachable, say so and catch the board up next turn.
- Parallel work runs in worktrees with explicit file ownership; the shared contract is committed first.
- Times are UTC epoch seconds; the XM server clock is New York + 7 h (`market/server_time.py`).
