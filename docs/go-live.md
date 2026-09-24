# Going live — runbook

FXCommand never sends an order on its own initiative during development, and no automated test sends an order to MetaTrader 5. **The first real order is placed by you**, by starting a Broker-mode Session from the dashboard. Take the three stages in order; each one is something you switch on deliberately.

What has been verified before you start:

- the whole engine on the simulated broker, including injected broker faults (requotes, timeouts, partial fills, zero-price replies, failed closes, a hung terminal) — `uv run pytest`, Playwright E2E;
- the real `Mt5Broker` adapter's order paths against a fake MetaTrader5 package (`tests/test_mt5_adapter.py`);
- read-only checks against your real terminal: account, hedging mode, quotes, bars, and our loss-at-stop and margin math against the terminal's own calculators; and a Paper Session running the full engine on the real feed with every order method tripwired (`uv run pytest -m mt5 -o addopts=""`).

What has **not** been exercised by anyone yet: a real order through your broker. Stage 2 is where that happens, on demo.

## Stage 0 — once

1. MetaTrader 5 running and logged in; **Algo Trading** button on (toolbar).
2. `cd frontend; npm run build`, then in PowerShell `cd backend; $env:BROKER="mt5"; uv run fxcommand` — or simply `scripts\run-fxcommand.ps1`.
3. Settings → **Alerts to Telegram**: create a bot with @BotFather, send it any message, paste the bot token and your chat id, **Send test**. You should receive the test message on your phone.
4. Account page → **Pre-flight check**: everything green except items you understand (e.g. "risk % can buy the minimum lot" on a small account).
5. Optional: `powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1` so the app starts at logon and restarts after a crash. The PC is kept awake while a Session is active; Windows Update restarts are not prevented — schedule them outside trading hours.

## Stage 1 — Paper on the real feed

1. Sessions → New session → **Execution: Paper**. Same Strategies, Timeframes and Risk Profiles you intend to trade.
2. Start it. The start dialog says *PAPER — nothing is sent to the account*.
3. Let it run for at least a few days. Check: Positions (PAPER badge), History, Journal ("PAPER Opened …", Risk Gate rejections and their reasons), the daily Telegram summary.
4. Paper fills are at bid/ask + 1 point; stops are checked on every M1 bar and tick. Real fills can be worse (slippage, requotes) — Paper is an upper bound, not a promise.

## Stage 2 — Broker execution on the demo account

1. Stop the Paper Session, switch it to **Execution: Broker** (or create a new one).
2. Start it. On demo the pre-flight is advisory; read it anyway.
3. Watch the first orders in the Journal and in the terminal (Trade tab — the comment is `fxc s<id> <strategy>`, the magic number is the Session's). Compare the Journal's entry price, SL and TP with the terminal.
4. Try the controls while a position is open: Pause (entries stop, the position is still managed), Stop with and without "close positions", and the **Kill Switch**.
5. Things the app handles and tells you about (Journal + Telegram): requotes (one re-checked retry), uncertain orders (never retried; any resulting position is adopted), positions opened elsewhere with a Session's magic number (Orphan adopted), failed closes (retried for 5 s, then an alert), a terminal disconnect longer than 60 s, a terminal logged into another account (every Session becomes Interrupted).

With the $52 demo balance and 1% risk, most signals are rejected for size (`volume_min`): the minimum 0.01 lot fits only stops of about 5 pips. That is the Risk Gate working. Either fund the account, raise the risk % knowingly, or enable **Allow the minimum lot** on a Risk Profile with a cap you accept (the Journal shows the real risk taken).

## Stage 3 — a live account

1. Log the terminal into the live account. Every Session that was active is Interrupted (Pinned Login).
2. Account page → **Live trading** → type the account number. This arms the **Equity Floor** at 80% of current equity: below it the Kill Switch fires and live Sessions cannot start until you reset it (Risk page, typed account number).
3. Risk page → **Live Caps** (default: at most 1% risk per trade and 0.10 lots per order, whatever a Risk Profile says). Changing them on a live account needs the typed account number.
4. Start small: one Session, one or two Symbols, the lowest risk you can trade. Auto-promotion by the Learning system never happens on a live account; auto-rollback does.
5. The start dialog's blocking checks must pass: terminal connected, Algo Trading on, EA trading allowed, hedging account, ping < 500 ms, live-enabled, Equity Floor not breached, each Symbol tradable with a fresh quote and a spread within its Risk Profile, margin for the minimum lot, database writable, engine loop running.

## Every day

- Telegram daily summary (sent when the server day rolls over).
- Overview → alerts; Risk page → daily loss used.
- After a restart: Sessions come back **Interrupted** — restart them yourself after a look at Positions.

## If something goes wrong

- **Kill Switch** (top bar): stops every Session, then closes every position with a FXCommand magic number, retrying until the terminal confirms. If some cannot be closed, the alert says so — close them in the terminal.
- Positions left open by a stopped Session are protected by their server-side SL/TP (Broker mode only; Paper positions are always closed on stop).
- Foreign positions (manual trades, other EAs) are never touched.
