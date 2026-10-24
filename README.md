# Vibe Desk

**Chat a trading idea, get an honest verdict on real data.**

Vibe Desk is a local research desk for GOLD and the FX majors on MetaTrader 5. You describe an idea in plain words: "buy GOLD when it breaks the 20-bar high on H1, stop 2 ATR, target 2R". An AI turns it into a strategy built from a fixed set of building blocks. Deterministic Python code then tests that strategy on about twelve years of real broker history and tells you whether it holds up: **"Looks real"** or **"No edge"**, with every number behind the call.

The AI only writes strategies. It never trades, and nothing in this app can place an order.

> Results on past data don't promise future ones. This is a research tool, not financial advice.

## Features

- **Chat to strategy.** Ideas in any language become a validated Strategy Spec. If the AI gets a parameter wrong, the validation errors go back to it until the Spec is valid.
- **Tested on years it never saw.** Walk-forward over 2017 to 2024: each year is traded with the settings that did best on the three years before it.
- **Real costs.** The spread charged is the larger of the bar's spread and the symbol's measured floor; slippage is charged on every fill, and swaps as measured.
- **A hard pass bar.** The strategy has to pass every check:
  - profit factor, trade count and profitable years;
  - profit factor at double the spread;
  - beating 1,000 random trade sets;
  - a Sharpe ratio corrected for luck, and the chance it's overfit;
  - profit not coming from five lucky trades;
  - nearby settings, longs and shorts, and quiet and busy markets all holding up.
- **Iterate in the chat.** Each result goes back to the AI, so "make it trade more" or "only longs" builds on the last test.
- **Saved chats.** Recents in the sidebar reopen any conversation with its strategies and results.
- **One-click start** on Windows, light and dark themes, and layouts for desktop and phone.
- **Every test recorded.** Tests go into an append-only SQLite Ledger.

## How it works

```
your idea ──► AI (OpenAI-compatible API) ──► Strategy Spec (blocks + parameter grid)
                                                   │
                     real XM history (Parquet) ──► Gauntlet: walk-forward, costs, checks
                                                   │
                                     Verdict, numbers, charts ──► back into the chat
```

- **Blocks:**
  - entries: Donchian breakout, moving-average cross, RSI threshold, Bollinger touch, pullback to a moving average, session range breakout;
  - filters: moving-average slope, ADX, ATR regime, session window, day of week;
  - exits: ATR stop, R target, ATR target, ATR trailing stop, time exit, opposite signal;
  - sizing: fixed risk.
- **Symbols:** GOLD, EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF, NZDUSD, on M15, H1, H4 or D1.
- **Results are in R:** profit in units of the stop distance.
- **2025 onward is held out:** it's kept as a final check, and ordinary tests never see it.

## Requirements

- Windows (the MetaTrader 5 Python package is Windows-only)
- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- MetaTrader 5, logged in to your broker and receiving quotes, to import history. Access is read-only. Built against XM, whose server clock runs on European EET/EEST (Europe/Athens) with the New York trading week.
- An OpenAI-compatible chat API for the AI: OmniRoute, OpenAI, OpenRouter or any server with `/v1/models` and `/v1/chat/completions`.

## Quick start

```bash
git clone https://github.com/marknatdev/fxcommand.git
cd fxcommand
uv sync --extra mt5

# With MetaTrader 5 open and logged in:
uv run desk smoke      # check the terminal: symbols, live tick, a little history
uv run desk import     # import every symbol and timeframe from 2014 (a few minutes, ~570 MB)
uv run desk floors     # measure each symbol's spread floor and swaps

# Start the app on http://127.0.0.1:8765
uv run --extra mt5 vibedesk-web --open
```

Open **Settings** in the app and enter your API URL (for example `https://api.openai.com/v1`), your key and a model. They're saved to `data/settings.json`. You can also set them in `.env`; see `.env.example`.

`Start Vibe Desk.bat` is the owner's one-click launcher. It opens an SSH tunnel to the owner's OmniRoute server, starts the app and opens the browser. Edit or drop the tunnel line for your own setup.

## Using it

1. Type an idea and press Enter. The AI answers with a strategy card: its entry, filters, exits, and how many parameter settings it will try.
2. Press **Run test**. In about ten seconds the result panel opens with:
   - the verdict;
   - trades, net R, profit factor, win rate and drawdown;
   - the cumulative-R curve and net R per year;
   - every check.
3. Say what to change, or start a **New idea**. **Update data** in the sidebar refreshes the history and spreads from MetaTrader 5.

## Command line

| Command | What it does |
|---|---|
| `uv run desk smoke` | Read-only check of the terminal: symbols, the live tick against your clock, a day of bars |
| `uv run desk import [--symbols …] [--start YYYY-MM-DD]` | Import history into `data/bars` |
| `uv run desk refresh` | Re-import the latest chunks |
| `uv run desk floors` | Measure spread floors and swaps from the last 14 days of ticks |
| `uv run desk data-report` | First usable month, masked days, and clock checks per year |
| `uv run vibedesk-web [--port N] [--open]` | The web app |

## Development

```bash
uv run pytest                             # default suite (~45 s, 4 workers)
uv run pytest -m slow                     # known-answer tests and benchmarks (~4 min)
uv run ruff check src tests && uv run ruff format --check src tests
uv run python tests/demo_server.py 8766   # the app with a canned AI and a throwaway Ledger
```

The tests never touch MetaTrader 5 or the network. They use a fake terminal and synthetic bars, and guard tests fail if any code names an order call.

```
src/vibedesk/
  app/        web app: FastAPI server, the page, AI client, chats, settings
  blocks/     the Block Library and Strategy Specs
  engine/     numba fill engine, costs and swaps, metrics in R
  gauntlet/   walk-forward, coin-flip baseline, statistics, checks, Trials, the Holdout
  ledger/     the append-only SQLite Ledger
  market/     MT5 seam (read-only), server clock, Parquet cache, data-quality mask, costs, importer
  cli.py      the `desk` commands
tests/        ~1,240 tests, including an independent twin of the engine
```

`data/` (bars, costs, the Ledger, chats and settings) stays on your machine and is not in git.

## Documentation

- [AGENTS.md](AGENTS.md): the full handoff, covering status, architecture, data, known issues and traps
- [CONTEXT.md](CONTEXT.md): the glossary (Spec, Block, Trial, Verdict, Holdout, Spread Floor, R, …)
- [docs/spec/layer-a-verdict-engine.md](docs/spec/layer-a-verdict-engine.md): the test engine's specification
- [docs/adr/](docs/adr/): design decisions
- [docs/intent/vibe-desk.md](docs/intent/vibe-desk.md): why this exists

## Status

Working: the web app, real data for all eight symbols, the test engine and saved chats.

Known issues:
- XM's clock appears to switch on European daylight-saving dates, so about four weeks a year sit an hour off. This matters for session-based ideas.
- The AI's prompt hasn't been tuned against a real model yet.

Not built yet:
- a button to check a strategy on 2025 to today;
- Telegram alerts and an MCP server;
- forward testing on a demo account.
