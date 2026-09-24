# FXCommand

Autonomous multi-symbol trading for MetaTrader 5, run and controlled from one dashboard.

- **Sessions** trade a set of symbols. Each symbol is an *Assignment*: one Strategy + Timeframe + Risk Profile. Sessions are started, paused, stopped (leave or close positions) and edited from the dashboard. Several Sessions can run at once.
- **Strategies (v1):** EMA Cross (optional trend filter), Donchian Breakout, RSI Mean Reversion (ADX-gated). Every trade uses ATR-based stop-loss and take-profit.
- **Risk Gate** runs before every order. It sizes the position by risk % and checks:
  - maximum spread and stale quotes
  - Trading Window
  - maximum positions per Session and across all Sessions
  - daily loss limits per Session and across all Sessions; breaching one auto-stops trading
  - the live-account gate
  - the broker's stop level

  Breakeven and ATR trailing stops are optional.
- **Kill switch** stops everything and closes every position FXCommand opened. It never touches manual trades or trades from other EAs, which are told apart by magic number.
- **Journal** records every signal, risk decision (including why a trade was *not* taken), order and close.
- **Self-improving strategies (Learning page):** every Symbol + Timeframe is an *Arena* where the live *Champion* and up to three *Challengers* shadow-trade the same real bars under the live rules (never sent to the broker).
  - A walk-forward *Optimizer* proposes Challengers when a session stops, every server day, or on demand.
  - A Challenger replaces the Champion only when it passes every *Guardrail*: 30+ shadow trades, beats the Champion on the same bars, a deflated out-of-sample score, robustness, and drawdown.
  - Promotions apply when the position is flat, are versioned, and can be rolled back; a failing Promotion rolls back automatically.
  - A *Signal Filter* learns from every closed trade which signals tend to lose, and blocks them only once it has proven held-out lift.
  - Auto-promotion is opt-in and never allowed on a live account.
- **Pages:** Overview, Sessions (with detail and editor), Symbols, Strategies, Learning, Risk, Positions, History & Journal, Account, Logs, Settings.
- **MCP server** lets AI agents read status and start, pause or stop Sessions (`.mcp.json`).

## Quick start (simulated market)

```bash
cd backend && uv sync
cd ../frontend && npm install && npm run build
cd ../backend && uv run fxcommand         # http://127.0.0.1:8000
```

The simulator runs 60× real time by default. You can change the speed, advance bars or inject price shocks under **Settings**.

## Against MetaTrader 5

1. Start MT5, log in, and enable **Algo Trading**.
2. From `backend/`, run `$env:BROKER="mt5"; uv run fxcommand` (PowerShell) or `BROKER=mt5 uv run fxcommand` (bash). The app attaches to the running terminal and never stores credentials.
3. Demo accounts can trade right away. Live accounts stay blocked until you enable live trading on the **Account** page by typing the account number.

## Tests

```bash
cd backend && uv run pytest                       # engine, risk, strategies, API (simulated broker)
cd backend && uv run pytest -m mt5 -o addopts=""  # read-only smoke against the real terminal
cd e2e && npm install && npx playwright install chromium && npm test
```

See `CONTEXT.md` for the domain language and `docs/adr/` for design decisions.
