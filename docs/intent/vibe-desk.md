# Intent: Vibe Desk

Confirmed with the owner on 2026-10-21 (interview-me).

- **Outcome:** A fresh "vibe trading" desk. The owner types a trading idea in chat. The AI researches it, writes and backtests a strategy, and returns an honest verdict: out-of-sample results, cost and spread stress, and a comparison against a coin-flip baseline. Strategies that pass run on their own, on paper first and then on the XM demo.
- **User:** The owner alone, on the laptop, trading GOLD and FX on MT5 (XM).
- **Roles:** The AI researches and builds strategies. Deterministic code executes them, behind a hard Risk Gate and a kill switch. There is no LLM call per candle, and the AI never sends orders from chat.
- **Surfaces:** A local web app (chat, strategy cards with verdicts, positions, kill switch), Telegram alerts with a daily summary, and an MCP server so the same work can be driven from Claude Code.
- **LLM:** OmniRoute behind one seam, with a fake adapter for tests.
- **Success (3 months):** The owner tests about 10× more ideas, gets a verdict they trust within minutes, and stops fooling themselves. Profit is not promised.
- **Money path:** Paper on the real feed, then the XM demo, then live. Live needs a hard gate (for example 4 weeks, 100+ trades, PF ≥ 1.2, drawdown < 6%), and only the owner can switch it on.
- **Constraints:**
  - The code is new; nothing is reused from `D:\fxcommand`.
  - Tests and agents never send orders.
  - nofx (AGPL) contributes patterns only, never copied code.
- **Out of scope:**
  - An nofx-style autopilot where an LLM decides every trade.
  - Crypto exchanges and stocks.
  - The AI promoting a strategy to live on its own.
  - Multiple users or a login.
  - A server or Linux deployment.
  - The AI increasing position size.

## Context

- **Earlier builds:** Two earlier attempts (`D:\fxcommand`, and an earlier `D:\millmaker\fxcommand` that is now empty) both found that the missing edge was the problem, not the infrastructure. GOLD scalping lost to the spread. H4 trend breakouts made only about +0.1R per trade after costs.
- **Reference repos:**
  - [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) (MIT)
  - [OpenByteInc/QuantDinger](https://github.com/OpenByteInc/QuantDinger)
  - [NoFxAiOS/nofx](https://github.com/NoFxAiOS/nofx) (AGPL-3.0)
