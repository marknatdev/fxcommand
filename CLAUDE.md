# FXCommand — project notes for Claude

Autonomous multi-symbol trading engine for MetaTrader 5 with a dashboard. Domain language lives in `CONTEXT.md` (Session, Assignment, Risk Gate, Magic Number, Kill Switch, …) — use those terms exactly. Architecture decisions are in `docs/adr/`.

## Layout

- `backend/` — Python 3.10 (uv). One process = engine + FastAPI + WebSocket + built dashboard.
  - `fxcommand/broker/` — the **only** market seam: `Broker` protocol, `SimBroker` (seeded in-memory market), `Mt5Broker` (real terminal). Every call goes through `BrokerThread` (one thread; the MT5 package is not thread-safe).
  - `fxcommand/strategies/` — pure `bars + params -> Signal`. `risk/` — pure Risk Gate (`check`, `manage`) and Trading Window.
  - `fxcommand/engine/session_manager.py` — the deep core: lifecycle, bar-close evaluation, ownership by magic, auto-stop, kill switch, boot → Interrupted.
  - `fxcommand/learning/` — self-improvement (ADR 0004/0005): `paper.py` (Shadow Trade rules, `backtest`), `optimizer.py` (Walk-forward Optimizer Run, pure), `objective.py` (R stats, Guardrails, calibration constants), `signal_filter.py` (logistic regression, observe → no_edge/active → disabled), `service.py` (LearningService: the engine's only learning dependency), `repo.py` (learning tables).
  - `fxcommand/api/routes.py` — HTTP per dashboard page + `/ws`; `/api/sim/*` test controls work only when `BROKER=sim`.
  - `fxcommand/mcp_server.py` — stdio MCP server (registered in `.mcp.json`), a thin client of the HTTP API.
- `frontend/` — React 19 + Vite + TS + Tailwind v4 + TanStack Query + lightweight-charts. One page per section + Overview.
- `e2e/` — `@playwright/test` specs driving the real app against `SimBroker` with the clock stopped.

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
```

Env: `BROKER` (sim|mt5), `FXC_DB`, `FXC_PORT`, `FXC_SIM_SPEED` (0 = clock stopped), `FXC_SIM_SEED`, `FXC_SIM_START`, `MT5_PATH`, `FXC_STATIC`, `FXC_ENGINE=0` (API without engine loop).

## Rules

- **Never place, modify or close an order through `Mt5Broker`** from tests, scripts or agents — not even on demo. Order-placing tests run on `SimBroker` only; the MT5 smoke test is read-only (its order methods are a tripwire). Real trading is started by the operator from the dashboard.
- Run the server with exactly one worker and no `--reload` (ADR 0001): a second process means a second engine.
- Market-dependent Risk Gate checks and the order send happen in ONE `broker.run(...)` call (`_enter`, `_manage_positions`) so the gate prices stops against the tick the order is sent at. Keep it that way.
- A Symbol appears at most once per Session and in at most one running Session (ADR 0003).
- Learning: every engine → learning call goes through `SessionManager._learn()` (exceptions are swallowed and journaled once — learning must never stop trading). LearningService never places orders. Learning records are keyed by Arena (symbol, timeframe) or (session_id, symbol) — never by assignment id (it changes on every edit). New learning concepts go in NEW tables (SQLite `create_all` does not add columns to existing DBs).
- Strategies are vectorised (`compute` → `signals()`); live `run()` reads the last row of a window with `WARMUP_BARS` of warm-up. The golden fixture (`tests/fixtures/golden_signals.json`) pins per-bar behaviour — regenerate it only on a deliberate behaviour change.
- Auto-promotion is never allowed on a live account (checked when queuing and again when applying); auto-rollback is allowed everywhere. Optimizer Runs execute in a separate process in the app (`LearningService(processes=True)`, entry point `optimizer.optimize_job` — keep it picklable, no lambdas); tests use a thread. Guardrail constants live in `learning/objective.py` and are pinned by the noise/planted-edge tests — change them only with those tests.
- Timestamps are broker server time as epoch seconds (XM = UTC+3); the UI labels them "server time".
- Keep `CONTEXT.md` a glossary only; record hard-to-reverse decisions as ADRs.

## Verification before calling work done

1. `uv run pytest` green. 2. `npm run build` (type-check) clean. 3. `cd e2e && npm test` green. 4. Exploratory pass with `playwright-cli` over every page (console errors, mobile width overflow). 5. `uv run pytest -m mt5 -o addopts=""` if the broker adapter changed.
