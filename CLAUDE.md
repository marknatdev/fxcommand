# FXCommand — project notes for Claude

Autonomous multi-symbol trading engine for MetaTrader 5 with a dashboard. Domain language lives in `CONTEXT.md` (Session, Assignment, Risk Gate, Magic Number, Kill Switch, …) — use those terms exactly. Architecture decisions are in `docs/adr/`.

## Layout

- `backend/` — Python 3.10 (uv). One process = engine + FastAPI + WebSocket + built dashboard.
  - `fxcommand/broker/` — the **only** market seam: `Broker` protocol, `SimBroker` (seeded in-memory market, fault injection), `Mt5Broker` (real terminal), `PaperRouter`/`PaperBook` (Paper Execution Mode, ADR 0007). Every call goes through `BrokerThread` (one thread; the MT5 package is not thread-safe; calls time out with `BrokerTimeout`).
  - `fxcommand/strategies/` — pure `bars + params -> Signal`. `risk/` — pure Risk Gate (`check`, `manage`) and Trading Window.
  - `fxcommand/engine/session_manager.py` — the deep core: lifecycle, bar-close evaluation, ownership by magic, auto-stop, kill switch, boot → Interrupted, plus going live: Order Outcomes, Orphan adoption, close-until-confirmed, Pinned Login, Live Caps, Equity Floor, Weekend Close. `engine/preflight.py` — pure Pre-flight Check.
  - `fxcommand/notify/` — Telegram Notifier (`AlertDispatcher` listens to the EventBus; outbox on the Settings page).
  - `fxcommand/learning/` — self-improvement (ADR 0004/0005): `paper.py` (Shadow Trade rules, `backtest`), `optimizer.py` (Walk-forward Optimizer Run, pure), `objective.py` (R stats, Guardrails, calibration constants), `signal_filter.py` (logistic regression, observe → no_edge/active → disabled), `service.py` (LearningService: the engine's only learning dependency), `repo.py` (learning tables).
  - `fxcommand/api/routes.py` — HTTP per dashboard page + `/ws`; `/api/sim/*` test controls work only when `BROKER=sim`.
  - `fxcommand/mcp_server.py` — stdio MCP server (registered in `.mcp.json`), a thin client of the HTTP API. Its tool set is pinned by `tests/test_review_api.py::test_the_mcp_tool_set_is_pinned`: never add a tool that overrides the Cost Check, resets the Paper Account, starts an Evidence Run, writes trials or the holdout, enables live, edits risk or promotes.
- `frontend/` — React 19 + Vite + TS + Tailwind v4 + TanStack Query + lightweight-charts. One page per section + Overview.
- `e2e/` — `@playwright/test` specs driving the real app against `SimBroker` with the clock stopped.
- `scripts/` — `run-fxcommand.ps1` (BROKER=mt5, restart after crash, logs to backend/data/logs), `install-autostart.ps1` / `uninstall-autostart.ps1` (scheduled task at logon), `run-gold-review.ps1` (the weekly Strategy Review, headless, restricted tools, in its own worktree) and `install-review-task.ps1` / `uninstall-review-task.ps1` (Saturdays) — the operator runs the install scripts, not Claude.
- `backend/fxcommand/research_cli.py` — `fxcommand-research`: the Strategy Review's research tool (snapshot, backtest → ledger, ledger, holdout --confirm), an HTTP client of the app. `.claude/skills/gold-review/SKILL.md` — the review protocol.
- `docs/go-live.md` — the operator's runbook: Paper → Broker on demo → live.

## Commands

```bash
cd backend && uv sync                                  # first time
cd frontend && npm install && npm run build            # dashboard -> frontend/dist (served by the backend)
cd backend && uv run fxcommand                         # BROKER=sim by default -> http://127.0.0.1:8000
BROKER=mt5 uv run fxcommand                            # attach to the logged-in MT5 terminal
cd backend && uv run pytest                            # unit + integration (sim only)
cd backend && uv run pytest -m mt5 -o addopts=""       # READ-ONLY smoke against the real terminal
cd e2e && npm install && npx playwright install chromium && npm test   # builds UI, runs E2E on port 8765
cd frontend && npm run dev                             # UI dev server on :5173, proxies to :8000
cd backend && uv run fxcommand-research backtest GOLD H4 --strategy trend_breakout --param entry=40 --hypothesis "..."   # research (needs the app on :8000; always recorded)
```

Env: `BROKER` (sim|mt5), `FXC_DB`, `FXC_PORT`, `FXC_SIM_SPEED` (0 = clock stopped), `FXC_SIM_SEED`, `FXC_SIM_START`, `FXC_SIM_DEEP_DAYS` (H1 history before the M1 history, default 600), `MT5_PATH`, `FXC_STATIC`, `FXC_ENGINE=0` (API without engine loop), `FXC_KEEP_AWAKE` (default 1 with BROKER=mt5).

## Rules

- **Never place, modify or close an order through `Mt5Broker`** from tests, scripts or agents — not even on demo. Order-placing tests run on `SimBroker` only; the MT5 smoke test is read-only (its order methods are a tripwire). Real trading is started by the operator from the dashboard.
- Run the server with exactly one worker and no `--reload` (ADR 0001): a second process means a second engine.
- Market-dependent Risk Gate checks and the order send happen in ONE `broker.run(...)` call (`_enter`, `_manage_positions`) so the gate prices stops against the tick the order is sent at. Keep it that way.
- One Magic Number per Assignment (ADR 0009, supersedes 0003): an Arena (symbol, timeframe) appears once per Session and in one running Session. Session-level checks use `store.session_magics(s.id)`; matching a position to an Assignment goes through `SessionManager._owner()` only — never `p.magic == s.magic`.
- Learning: every engine → learning call goes through `SessionManager._learn()` (exceptions are swallowed and journaled once — learning must never stop trading). LearningService never places orders. Learning records are keyed by Arena (symbol, timeframe) or (session_id, symbol, timeframe) — never by assignment id (it changes on every edit). New learning concepts go in NEW tables.
- Strategies are vectorised (`compute` → `signals()`); live `run()` reads the last row of a window with `WARMUP_BARS` of warm-up. The golden fixtures (`tests/fixtures/golden_signals.json`, `golden_gold_signals.json`) pin per-bar behaviour — regenerate them only on a deliberate behaviour change. They are generated on `sim.LEGACY_SYMBOLS`: never edit that tuple (change `DEFAULT_SYMBOLS` instead), or the pinned inputs move.
- Auto-promotion is never allowed on a live account (checked when queuing and again when applying); auto-rollback is allowed everywhere. Optimizer Runs execute in a separate process in the app (`LearningService(processes=True)`, entry point `optimizer.optimize_job` — keep it picklable, no lambdas); tests use a thread. Guardrail constants live in `learning/objective.py` and are pinned by the noise/planted-edge tests — change them only with those tests.
- Backtests and Shadow Trades price with a `CostModel` (price-scaled spread/slippage, swap per MT5 rollover) and judge the Trading Window at the fill through `EntryGate` (picklable — Optimizer Runs run in a process). Never price costs from one quote in the daily break: use `LearningService._typical_spread`. Research parity: `docs/research/strategies-2026-09/scripts/crosscheck_m4.py` (read-only, real history; run from the research workspace).
- Pending Entries (ADR 0008) are sent ONLY through `SessionManager._enter`, retried every engine pass, expire by `risk.window.fill_limit` after the observed Next Tradable Time, and are journaled once. A strategy close the market refuses (10018) becomes a pending exit, never an alert and never a reason to open the other side.
- The Paper `AccountInfo` (`_paper_account`) is for the Risk Gate's sizing and limits only — never assign it to `self._account` (the real Equity Floor, day start and snapshots read that). Paper limits stop only Paper Sessions. Paper tickets only go down; a reset archives by epoch.
- Evidence Runs (`learning/evidence.py`, `LearningService.request_evidence`) are read-only Backtests on the full feed history: pages of at most `CHUNK_BARS` through `closed_bars(..., offset)`, each its own short `broker.run`, read only while no order is in flight (`SessionManager._order_in_flight` wraps `_enter`, `_close_confirmed`, `_retry_exit` and feeds `learning.order_busy`). They queue behind Optimizer Runs in the learning pool (`evidence_job` must stay picklable). Every setting a result depends on goes into `EvidenceSpec.key` — add new ones there, or the badge shows Evidence for untested settings. Weekend Close is modelled in the Backtest through `risk.window.WeekendClose`, the same `in_weekend_close` the engine uses. Evidence stays in the local DB: never commit real-feed Evidence figures.
- Strategy Review research (`learning/research.py`): the Sealed Holdout starts at `research.holdout_from(server_time, timeframe)` — the only definition; the snapshot keeps bars that closed by then and `record_trial` refuses data that reaches it. A holdout evaluation reserves its ledger row before the backtest (one per Arena + Candidate key, final). Reviewer Challengers (`submit_challenger`) are judged by `optimizer.evaluate_job` (the Optimizer's own walk-forward and neighbours), must be in the Champion's family, never retire a Challenger to make room, and pay `max(ledger count, TOP_K)` trials in the Guardrail.
- The Claude Strategy Review (the `gold-review` skill, D41–D54) may only: read through the read-only MCP tools, run `fxcommand-research`, submit Challengers (`submit_challenger`, which needs a passed holdout) and reports (`submit_review`), and propose new strategy code as a PR from a `review/<date>-<slug>` branch. It never starts/stops Sessions, uses the Kill Switch, promotes, enables live, changes risk/Live Caps/Equity Floor, overrides the Cost Check, resets Paper, starts Evidence Runs, merges, or calls an order method. The scheduled run enforces this with an allow-list (`scripts/run-gold-review.ps1`, pinned by `tests/test_research_cli.py`): when adding an MCP tool, place it on the allowed or denied side there. Never regenerate existing golden fixtures in a review PR. PRs and commits carry no P&L, balances or account data.
- Every research backtest goes through `fxcommand-research` (it records the trial, pass or fail); research never reads bars past `research.holdout_from`. A new-code hypothesis is a `--script` defining `STRATEGY` (a new key) and optional `CONTEXT` + `prepare()`; other timeframes are aligned only through `strategies.mtf.align_closed`.
- The Cost Check is enforced in `_start` and when a Promotion applies (`cost_check`, `cost_check_max_r`, overrides per (session, symbol, timeframe)); it prices from `SpreadBook`, never one quote in the break. Tests that use the FAST M1 strategy lift the limit explicitly (Harness, e2e global setup) — there is no bypass.
- Going live (ADR 0006/0007/0008): only hedging accounts are traded; a Session is pinned to the login it started on. Order Outcomes: `filled` / `not_executed` (one re-gated retry, RETRY_ONCE retcodes) / `uncertain` (NEVER retry an entry — re-read positions and adopt Orphans). Closes go through `_close_confirmed` (retry until the terminal confirms). Never open the opposite side while a close failed. The Kill Switch marks Sessions stopped before taking the engine lock. Live Caps and the Equity Floor bind only Broker Sessions on a live account; Paper Sessions skip live/AutoTrading gates and are excluded from Account P&L and the real global limits (they have the Paper Account's own, ADR 0008).
- The Paper book only sees the real Broker through `ReadOnlyBroker` (order methods raise). Paper tickets are negative. Paper Sessions always close their positions on stop.
- Mt5Broker order paths are tested against `tests/fake_mt5.py` (no terminal). `tests/test_mt5_smoke.py` also runs a Paper Session on the real feed with order methods tripwired — still read-only.
- Store migrations: `Store._add_missing_columns()` adds new model columns (with their model default) to existing SQLite tables; new *concepts* still prefer new tables.
- Timestamps are broker server time as epoch seconds (XM = UTC+3); the UI labels them "server time".
- SimBroker models GOLD's daily break (23:57–01:00, orders answer market closed 10018, 70-point reopen spread) and refuses orders on weekends. Engine paths that retry every pass must journal a repeated failure once (see `_manage_positions`).
- Keep `CONTEXT.md` a glossary only; record hard-to-reverse decisions as ADRs.

## Verification before calling work done

1. `uv run pytest` green. 2. `npm run build` (type-check) clean. 3. `cd e2e && npm test` green. 4. Exploratory pass with `playwright-cli` over every page (console errors, mobile width overflow). 5. `uv run pytest -m mt5 -o addopts=""` if the broker adapter changed.
