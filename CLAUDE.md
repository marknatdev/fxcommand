# Vibe Desk

An AI research desk for one owner who trades GOLD and FX on MT5 (XM). The owner chats an Idea, the Research Desk builds a block-based Strategy Spec, and the Gauntlet gives an honest Verdict. Deterministic code executes; the AI never trades.

- **Handoff:** `AGENTS.md` has the whole picture (status, how the app works, data, AI, board, known issues, traps). Read it first and keep it current when something it describes changes.
- **Intent:** `docs/intent/vibe-desk.md`. **Spec:** `docs/spec/layer-a-verdict-engine.md`. **Glossary:** `CONTEXT.md` (use its terms exactly). **Decisions:** `docs/adr/`.
- **Current work:** `tasks/plan.md` (definitions the tests depend on) and `tasks/todo.md` (tasks, results, checkpoints). Finished milestones: `tasks/done/`.

## Commands

**Start the app:** double-click `Start Vibe Desk.bat` (or the desktop shortcut "Vibe Desk"). It opens the SSH tunnel to OmniRoute on the Ubuntu server, starts the server on http://127.0.0.1:8765 and opens the browser. The OmniRoute key goes in the app's Settings (saved to `data/settings.json`) or in `.env` (see `.env.example`). "Update data" imports or refreshes every symbol from MT5 (read-only) and re-measures spreads.

```bash

uv sync                                   # dependencies (add --extra mt5 for the live terminal)
uv run --extra mt5 vibedesk-web --open    # the app, by hand (port 8765)
uv run python tests/demo_server.py        # the app on port 8766 with canned AI and throwaway Ledger/settings
uv run pytest                             # default fast suite, 4 workers, excludes slow & e2e (~30 s)
uv run playwright install chromium        # install Chromium for the Playwright E2E suite
uv run pytest -m e2e -n 0                 # Playwright E2E suite on deterministic fakes (~25 s)



uv run pytest -m slow                     # known answers, benchmarks, 12-year import (~4 min)
uv run pytest -n 0 tests/test_x.py        # one file, one process
uv run ruff check src tests
uv run ruff format --check src tests
uv run python tests/test_rules_version.py # print the rules fingerprints (re-pin)
uv run desk smoke | import | refresh | floors | data-report # read-only market commands (M3)
uv run vibedesk-mcp                       # stdio MCP server (8 tools, spec §8.2)
powershell -ExecutionPolicy Bypass -File scripts/setup-telegram.ps1 # Telegram push setup
```

### Registering the MCP server

The stdio MCP server (`vibedesk-mcp`) exposes 8 tools (5 read, 3 act; spec §8.2). It connects to the running HTTP API on `http://127.0.0.1:8765` over httpx. It contains zero owner tools and cannot approve Holdouts, extend budgets, or update settings.

1. **Claude Code CLI:**
   ```bash
   claude mcp add vibedesk -- uv --directory D:\millmaker\fxcommand run vibedesk-mcp
   ```
   Or in project `.mcp.json`:
   ```json
   {
     "mcpServers": {
       "vibedesk": {
         "command": "uv",
         "args": ["run", "vibedesk-mcp"],
         "env": {
           "VIBEDESK_API_URL": "http://127.0.0.1:8765"
         }
       }
     }
   }
   ```

2. **Claude Desktop:**
   In `%APPDATA%\Claude\claude_desktop_config.json`:
   ```json
   {
     "mcpServers": {
       "vibedesk": {
         "command": "uv",
         "args": [
           "--directory",
           "D:\\millmaker\\fxcommand",
           "run",
           "vibedesk-mcp"
         ],
         "env": {
           "VIBEDESK_API_URL": "http://127.0.0.1:8765"
         }
       }
     }
   }
   ```

## Layout

- `src/vibedesk/app/`: the web app: `server.py` (FastAPI and 15 endpoints), `static/index.html` (4-page app: Desk, Ideas, Ledger, Settings), `research.py` (Idea to Spec through AI), `llm.py` (OmniRoute client), `desk.py` (Trials, history, data updates), `settings.py`, `scheduler.py` (nightly refresh, weekly floors, daily digest).
- `src/vibedesk/desk/`: Research Desk: `loop.py` (SSE desk loop), `tools.py` (7 desk tools), `grounding.py` (Grounding Rule validator & placeholder renderer), `budget.py` (daily token budget).
- `src/vibedesk/mcp/`: stdio MCP server (`server.py`, `client.py` over HTTP API).
- `src/vibedesk/notify/`: Telegram push notifications and 21:00 Bangkok daily digest (`RecordingNotifier`, `TelegramNotifier`).
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

- Commit as you go, without asking; commit messages end with the `Co-Authored-By` line. Push (`origin` = github.com/marknatdev/fxcommand, `main`, public) only when the owner asks.
- Plans and results go in `tasks/`.
- The Vibe Kanban board was sunset by the owner on 2026-10-28; do not use it or the vibe_kanban MCP; track work in tasks/ (source of truth) and the repo-root kanban.md snapshot (card ids like CCC-85 stay as labels).
- Parallel work runs in worktrees with explicit file ownership; the shared contract is committed first.
- Times are UTC epoch seconds; XM's server clock is European EET/EEST (`Europe/Athens`), while the market trading week runs on New York time (`market/server_time.py`).
