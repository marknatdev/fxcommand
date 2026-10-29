# Vibe Desk: handoff for agents

Read this first. It is the whole picture as of 2026-10-23: what the project is, what works, how to run and change it, and what is still open. `CLAUDE.md` holds the short rules; the detail lives here.

## 1. What it is

A local web app for one owner who trades GOLD and FX majors on MT5 with the broker XM. The owner chats a trading idea in plain words. An AI (through OmniRoute) turns it into a **Strategy Spec** built from a fixed library of **Blocks**. Deterministic Python code then tests the Spec on about 12 years of real XM history and gives an honest **Verdict**. The AI only writes Specs; it never trades, and nothing in the app can place an order.

The owner wants fast answers about ideas, not more process. On 2026-10-23 they dropped the earlier rigor rules ("make it simple, done now"). Favour shipping working features over adding guards, checkpoints or reviews.

## 2. Status at a glance

| Area | State |
|---|---|
| Web app (4-page UI: Desk, Ideas, Ledger, Settings) | Built (CCC-52, CCC-56). Claude.ai-like design, live Verdict Cards with SVGs, owner-only auth cookie (`vibedesk_owner=1`), mobile-ready (375 px). Owner reviewing. |
| Real data | All 8 symbols imported from the owner's XM terminal, read-only, through 2026-10-22; spreads and swaps measured. |
| AI (OmniRoute) & Research Desk | Configured by the owner; desk loop with 7 desk tools (CCC-44), Grounding Rule validation and rendering (`desk/grounding.py`, CCC-45), and TokenBudget daily cap/warning (CCC-46). Real model calls await server memory restart (CCC-90). |
| Test engine (Gauntlet, Ledger) | Done (M1 to M3), full test suite green (>1,480 tests passing). |
| Telegram alerts, live trading | Telegram notifier built (CCC-57, CCC-59) with CANDIDATE, PASS, HOLDOUT FAIL notices and daily digest. Live EA parked in kanban.md (CCC-82). |
| MCP server (`vibedesk-mcp`) | Built (CCC-58, CCC-60). Stdio MCP server calling HTTP API (127.0.0.1:8765) over httpx, 8 tools (5 read, 3 act), no owner cookie / no approve/budget/settings tools. |
| Whole-app test suite (M7) | Complete (CCC-62, CCC-63, CCC-64). Playwright E2E whole-app journey green on fakes; exploratory click-through clean at desktop and 375 px mobile (0 errors, 0 overflow). Layer A fully built except owner-only items. |

First real results: a GOLD H1 Donchian breakout was "No edge" (1,184 blind trades, profit factor 0.80, 7 of 8 years losing); an EURUSD H4 RSI bounce never traded (too few trades to pick settings).

## 3. Run it

The owner double-clicks the desktop shortcut **"Vibe Desk"**, which runs `Start Vibe Desk.bat`:
1. if the app already answers on port 8765, it only opens the browser;
2. otherwise it opens an SSH tunnel to OmniRoute (only needed when the URL is `http://127.0.0.1:20128/v1`), starts the server and opens http://127.0.0.1:8765.

**After changing Python code, restart the server**: the .bat reuses a running one. Stop the process listening on 8765 (and its parent `cmd` running the .bat), then start the .bat again. The page (`static/index.html`) is read on every request, so HTML/JS changes need only a browser reload.

```bash
uv sync --extra mt5                        # dependencies, with the MT5 package (Windows only)
uv run --extra mt5 vibedesk-web --open     # the app by hand, port 8765
uv run python tests/demo_server.py 8766    # the app with a canned AI and a throwaway Ledger and settings, over the real bars
uv run pytest                              # default suite, 4 workers, ~45 s
uv run pytest -n 0 tests/test_app.py       # the app's tests
uv run pytest -m slow                      # known answers, benchmarks, 12-year import (~4 min)
uv run ruff check src tests && uv run ruff format --check src tests
uv run desk smoke | import | refresh | floors | data-report   # read-only MT5 data commands
uv run vibedesk-mcp                        # stdio MCP server for Claude Code / Claude Desktop
```

`.claude/launch.json` defines `vibedesk-demo` (port 8766) and `vibedesk` (port 8765) for the desktop app's browser preview. Use the demo server for click-throughs so the owner's history stays clean.

## 4. How the app works

1. **Chat.** `POST /api/draft {message, chat_id?}`: the server loads the chat so far, `research.draft_spec` sends it to the AI with a system prompt listing every Block (generated from the registry), parses the ```json block, validates it, and on errors feeds them back (up to 3 tries). The user message and the AI's answer (text + Spec) are saved to the chat.
2. **Test.** `POST /api/trial {spec, chat_id?}`: `Desk.run` validates the Spec, takes the symbol's latest measured cost, registers it in the Ledger and calls `gauntlet.trial.run_trial` (about 10 s). The page gets a summary; the chat gets a result message with a note that tells the AI the numbers next time.
3. **Carry on.** The next message in the same chat reaches the AI with the whole conversation, test results included, so "make it trade more" builds on the last Spec.

| Endpoint | What |
|---|---|
| `GET /` | the page |
| `GET /api/status` | data per symbol, data-update progress, settings (never the key) |
| `GET /api/models`, `POST /api/settings` | OmniRoute models; save URL, key, model |
| `GET /api/chats`, `GET /api/chats/{id}`, `DELETE /api/chats/{id}` | Recents, one chat, delete |
| `POST /api/draft`, `POST /api/trial` | above |
| `GET /api/trials`, `GET /api/trials/{id}` | past tests, one test's summary |
| `POST /api/data/update` | import or refresh every symbol from MT5 and re-measure spreads, in a background thread |

On start, `restore_old_trials` gives every test that has no chat (run before chats existed, or through the API without a `chat_id`) a chat of its own.

## 5. Code map

- `src/vibedesk/mcp/`: the stdio MCP server (`vibedesk-mcp`). Exposes 8 tools (5 read: list_ideas, get_verdict, get_ledger_stats, list_blocks, get_trial; 3 act: submit_idea, run_trial, request_holdout). Never carries the owner cookie and has no approve, budget, or settings tools. Register in Claude Code via CLI (`claude mcp add vibedesk -- uv --directory D:\millmaker\fxcommand run vibedesk-mcp`) or project `.mcp.json`; for Claude Desktop register in `claude_desktop_config.json`.
- `src/vibedesk/app/`: the app.
  - `server.py`: FastAPI, `create_app(root, llm=None, db=None, settings_root=None)` (tests pass a fake LLM and temp paths), `main()` for `vibedesk-web`.
  - `research.py`: Idea to Spec (`draft_spec`, `system_prompt`, `catalog`, `normalize`: the browser writes 2.0 as 2, and a param list mixing 1.5 and 2 is invalid, so numbers in a list are made one type).
  - `llm.py`: OmniRoute client (OpenAI-compatible `/models`, `/chat/completions`), retries 429/502/503/504.
  - `desk.py`: `Desk` (run, history, data status), `DataUpdate`, `summarize` (what the page shows).
  - `chats.py`: saved chats in `data/chats.db` (SQLite, editable), `ai_history`, `result_message`.
  - `settings.py`: `data/settings.json`, then `.env` / environment (`VIBEDESK_LLM_URL`, `_KEY`, `_MODEL`).
  - `static/index.html`: the single page (vanilla JS, Chart.js from cdnjs, fonts from Google Fonts with system fallbacks).
- `src/vibedesk/blocks/`: the Block Library and Strategy Specs (`spec.py`: `validate`, `expand_grid`, at most 50 combinations).
  - Entries: `donchian_breakout`, `ma_cross`, `rsi_threshold`, `bollinger_touch`, `pullback_to_ma`, `range_breakout`.
  - Filters: `ma_slope`, `adx`, `atr_regime`, `session_window`, `day_of_week`.
  - Exits: `atr_stop` (required), `r_target`, `atr_target`, `atr_trailing`, `time_exit`, `opposite_signal`. Sizing: `fixed_risk`.
- `src/vibedesk/engine/`: numba fill engine, costs and swaps, metrics in R.
- `src/vibedesk/gauntlet/`: walk-forward, Coin-Flip Baseline, statistics, Prosecutor, Pass Bar (`verdict.py`), `trial.py` (`run_trial`, Holdout functions).
- `src/vibedesk/ledger/store.py`: the SQLite Ledger (`data/desk.db`), append-only by triggers.
- `src/vibedesk/market/`: bars, the XM server clock (`server_time.py`), the Holdout guard (`data.py`), the MT5 seam (`mt5_api.py` with `real_api()` and `ReadOnlyMt5`, `fake_mt5.py`, `mt5.py`), the Parquet cache and data-quality mask (`cache.py`, `quality.py`), costs (`costs.py`), the importer, the data report.
- `src/vibedesk/cli.py`: the `desk` commands.
- `tests/`: `test_app.py` (the app), `demo_server.py` (click-through server), `reference_engine.py` (an independent twin of the engine), guard tests (no order calls, MT5 allowlist, Ledger has no UPDATE/DELETE except `app/chats.py`).

## 6. The test engine, briefly

- **Walk-forward:** blind years 2017 to 2024; for each, the Spec's settings that did best on the 3 years before are traded blind. A setting needs 30 training trades to be picked, else the year trades nothing.
- **Costs:** spread = max(bar spread, Spread Floor) + 1 point slippage per fill; swaps as measured today, a swap the position would earn counted as 0.
- **R:** profit in units of the stop distance.
- **Verdict:** CANDIDATE (shown as "Looks real") only if every check passes, else REJECT ("No edge") with the closest miss. Core checks: profit factor ≥ 1.2, trades ≥ 100, at least 5 of 8 profitable years, profit factor at 2× spread ≥ 1.05, beats 95% of 1,000 random coin-flip trade sets, Deflated Sharpe ≥ 0.95, PBO ≤ 0.3. The Prosecutor adds: top 5 trades ≤ 50% of profit, neighbour settings PF ≥ 1.0, longs and shorts PF ≥ 1.0, quiet/normal/busy regimes PF ≥ 0.9.
- **Ledger:** every test is a Trial in an Idea Family (one family per entry Block and symbol). The app extends a family's Trial Budget by itself. Rows are never changed; a Trial run by mistake stays (Trial #1 "GOLD breakout smoke test" was a speed test).
- **Holdout:** 2025-01-01 on is locked behind a Ledger grant and the app never uses it. A "check on 2025 to today" button would be the next honest step for a CANDIDATE.

## 7. Data

`data/` is not in git (about 570 MB of bars):
- `bars/{SYMBOL}/{TF}/…` Parquet chunks (M1 by month, others by year, e.g. `bars/GOLD/D1/2014.parquet`) and `bars/{SYMBOL}/meta.json` with the mask;
- `costs/{SYMBOL}.jsonl` spread and swap measurements;
- `desk.db` the Ledger; `chats.db` the chats; `settings.json` the OmniRoute settings (holds the key: never print, log or commit it).

Symbols: GOLD, EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF, NZDUSD. First usable month: GOLD 2014-06, FX 2014-01. Spread floors (2026-10-23): GOLD 55 points, EURUSD 20, GBPUSD 24, USDJPY 25, AUDUSD 24, USDCAD 30, USDCHF 26, NZDUSD 30.

Refresh with the app's "Update data" button or `uv run desk refresh` then `uv run desk floors`. The MT5 terminal (`C:\Program Files\MetaTrader 5`) must be running, logged in to XM and receiving quotes (`uv run desk smoke` checks the live tick). All MT5 access is read-only.

## 8. Rules and safety

- **No orders, ever.** `src/` reaches MT5 only through `real_api()`, which wraps it in `ReadOnlyMt5`; guard tests fail on any order or position call. Agents and tests never place trades, even on demo, and never start anything live. Only the owner does.
- Everything else from the old rule set (Holdout lock, every look is a Trial, rules versioning with fingerprint pins, served bars never change, XM clock checks) still runs and its tests stay green, but it no longer gates new work. Don't extend it unless the owner asks. If a change alters trades on purpose, bump `RULES_VERSION` in `src/vibedesk/rules.py` and re-pin `tests/test_rules_version.py` (`uv run python tests/test_rules_version.py` prints the new pins).
- Commit as you go without asking; messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` (or your own agent's line). Push only when the owner asks. Record plans and results in `tasks/todo.md`.
- GitHub: `origin` is https://github.com/marknatdev/fxcommand, branch `main`, **public**. This project replaced an older, unrelated fxcommand there on 2026-10-24 (owner's choice, no backup branch; the old project's `feature/*` branches are still on GitHub). Local `master` tracks `origin/main`. Never commit `data/` or `.env` (both ignored); the public files already name the server's address and tailnet host, by the owner's choice.
- The owner's Ubuntu server (`ubuntu@54.254.218.196`, key `D:\millmaker\ssh-key.pem`, tailnet `ip-172-26-4-45`) is shared with other projects (Hermes agents, Open WebUI, aipass, OmniRoute). Read-only checks only unless the owner asks for a change.

## 9. Known issues and open items, most important first

1. **OmniRoute answers 503 `resource_pressure`.** Root cause diagnosed in CCC-90: `omniroute.service` had `OMNIROUTE_MEMORY_MB=384` and `MemoryHigh=512M`. Startup briefly crossed 471 MiB (`cgroup_high`), and once in `critical`, `isRecovered()` requires V8 heap ratio <= 0.75 (294 MiB), but OmniRoute's idle V8 heap is ~300-325 MiB so it permanently latched in 503 rejection. Fix on the server: restart `omniroute.service` with raised limits in `~/.config/systemd/user/omniroute.service.d/override.conf` (`OMNIROUTE_MEMORY_MB=640`, `MemoryHigh=768M`, `MemoryMax=896M`, `OMNIROUTE_PRESSURE_SELF_RESTART=1`, `OMNIROUTE_PRESSURE_PSI_DISABLED=1`; see `tasks/todo.md` CCC-90). Then confirm one real draft end to end.
2. **XM's clock follows European daylight saving (fixed 2026-10-24, CCC-85).** XM's server runs on European EET/EEST (`Europe/Athens`), while the market trading week runs on New York time. `server_offset`, `server_to_utc`, and clock checks model this; the entire 12-year history across all 8 symbols was re-imported and passes all clock checks (103 of 104 symbol-years pass; only 2014 GOLD has a 1-day July 4 US holiday anomaly).
3. **The AI prompt is untested against the real model.** Watch for Specs with grids over 12 combinations, wrong param names, or prose that promises profit; tune `research.system_prompt`.
4. `desk import --start` mid-year: M1 starts at that month, other timeframes at 1 January, so that January is served without M1. The default start (2014-01-01) is fine.
5. Owner decisions left as they are: the Holdout counts a trade still open at data end at its last price; `atr_target` ignores the stop distance it receives.
6. **ADR 0005 (no digits in AI prose):** `src/vibedesk/desk/grounding.py` exposes `validate()` and `render()` (plus `GroundingError`); `src/vibedesk/desk/loop.py` validates prose, retries up to 2 times, then withholds the explanation (`grounding_withheld`) if ungrounded digits remain.
7. **Layer A complete, Layer B next:** Layer A is fully built across M1–M7 (core engine, gauntlet, MT5 data seam, research desk with token budget, HTTP API with scheduler, 4-page UI, Telegram notifier, stdio MCP server, and whole-app Playwright E2E suite). Owner-only items remain open: CCC-41 (M3 review), CCC-48 (20 sample ideas validation), CCC-79/80 (architectural decisions), CCC-83/87 (simple app & redesign review), CCC-90 (OmniRoute service restart). The next layer on the roadmap is Layer B (Forward Arena, demo gate, live runner parked as CCC-82).

## 10. The Vibe Kanban board (sunset)

The Vibe Kanban board was sunset by the owner on 2026-10-28. Do not use the `vibe_kanban` MCP or the local Vibe Kanban app (`vibe-kanban.exe`). Work is tracked in `tasks/` (source of truth) and `kanban.md` (the snapshot task list; card IDs like CCC-85 stay as labels).

## 11. Traps on this machine

- Windows 11; the shell tools are Git Bash and PowerShell. In Git Bash, arguments starting with `/` get rewritten to Windows paths (`/v1/x` became `C:/Program Files/Git/v1/x`): set `MSYS_NO_PATHCONV=1` or avoid leading slashes. `/tmp` in Git Bash and in Windows Python are different folders.
- Git has `core.autocrlf=true`; files may be LF or CRLF in the working tree. The Edit tool copes; for scripted edits keep each file's line endings.
- Install the MT5 package with `uv sync --extra mt5`; tests never need it (they use `FakeMt5`).
- numba compiles on first use, so the first Trial in a fresh process is slower.
- `tests/test_app.py::test_the_pages_script_parses` runs `node --check` on the page's script; a stray brace once stopped the whole page.
- Browser checks in the desktop app's preview pane: emulated viewports can render oddly; check layout with `getBoundingClientRect` and `scrollWidth` as well as screenshots. A modal `<dialog>` (Settings opens by itself when no key is saved) makes the rest of the page inert, so close it before clicking.

## 12. Documents

- `docs/intent/vibe-desk.md` (why), `docs/ideas/vibe-desk.md` (the locked one-pager), `docs/spec/layer-a-verdict-engine.md` (the engine's spec), `CONTEXT.md` (glossary: Spec, Block, Trial, Idea Family, Verdict, Holdout, Spread Floor, R, …).
- `docs/adr/0001` to `0007`: Block-composed strategies; the fixed Holdout; every evaluation is a Trial; the Spread Floor cost model; no digits in the AI's prose; the app is the single writer; the Pass Bar ignores win rate.
- `tasks/plan.md` and `tasks/todo.md` (M3 and everything since, including the simple app), `tasks/done/` (M1, M2).

## 13. History

- 2026-10-21: fresh start; intent, spec, glossary and ADRs written.
- 2026-10-22: M1 (Blocks, Specs, engine) and M2 (Gauntlet, Ledger, Trials, Holdout) done; known-answer tests pass (random walk always rejected, a planted edge found).
- 2026-10-23: M3 (MT5 read-only data: seam, clock, cache and mask, costs, importer, `desk` commands) done, plus a tuning pass and an independent review. The owner then dropped the rigor rules. In the same session: real import of all 8 symbols; the web app with AI chat and one-click start; the claude.ai-like redesign; saved chats; OmniRoute retries.
