# Pending Entries wait for the market; Paper Sessions share one notional Paper Account

Amends ADR 0007.

**Pending Entries.** A Signal the engine cannot act on now, for a reason that clears by itself, becomes a Pending Entry instead of a rejection. The reasons are: the market is shut (a stale quote, or the Broker answers "market closed"), the Trading Window is shut, or the spread is too wide.

- **Why.** GOLD is shut 23:57–01:00 every day. An H1/H4/D1 bar that closes at 00:00 produces its Signal while the market is closed. GOLD Reopen Drift's Signal always does. The backtests fill such Signals at the next tradable bar (decision D18), so rejecting them live would leave live trading and its evidence apart.
- **Storage.** A Pending Entry is persisted in `pending_entries`, keyed by Arena and bar, never by Assignment id.
- **Sending.** It is retried on every engine pass, not only at bar closes: Reopen's 5-minute fill window would otherwise always pass. It is only ever sent through `SessionManager._enter`. So every entry guard still applies: the Risk Gate and the order go in one broker call; the Kill Switch sequence; no opening while a close is outstanding; one position per Assignment; and Order Outcomes. An uncertain outcome is never retried.
- **Expiry.** The Next Tradable Time is observed: the first M1 bar printed with the Trading Window open, or a fresh quote with the window open. The entry expires the fill limit after it. The fill limit is the bar, or the Strategy's shorter Fill Window. The same `fill_limit` function decides this in every Backtest (`EntryGate`), so live and evidence agree. An entry that never sees the market open expires after four days.
- **Journal.** It is journaled once when deferred and once when it ends. The Learning Signal record gets only the final outcome.
- **Cancellation.** Stop, Kill Switch, Auto-stop, a Pinned Login change, a Champion change for the Arena and Weekend Close all cancel it.
- **Restarts and Pause.** It survives Interrupted: a Session restarted before expiry still sends it. A paused Session never sends.
- **Deferred exits.** A strategy exit or reversal that the market refuses because it is shut becomes a pending exit, retried every pass without an alert. The server-side stop-loss stays in place. No new side opens until the close is confirmed. The Paper book refuses such closes while the quote is stale, as the market would, so Paper and Broker go through the same path. A Paper Session's stop or Kill Switch still settles at once.

**Paper Account.** Paper Sessions size from one notional pool, `paper_account.start_balance` (default $5,000), not from the real Account. A $50 account would otherwise hit a $1.50 daily-loss cap on the first loss.

- **Equity** is the start balance, plus the realised Paper P&L of the current epoch, plus the open Paper P&L including swap.
- **The Paper `AccountInfo`** is built outside the broker call. It is used only for the Risk Gate's sizing and limits, and never stored as the engine's account. So the real Equity Floor, day start and equity snapshots never see Paper money.
- **Paper day start** is recorded at the server-day roll under `day_start:paper`.
- **Limits.** The session and global daily-loss limits apply to the pool, and a Paper limit stops only Paper Sessions.
- **Reset.** It needs the typed confirmation `RESET PAPER`. It is refused while a Paper Session is active or a Paper Position is open. It starts a new epoch: earlier rows are archived by epoch and never deleted. Paper tickets come from a counter that only goes down, so none is ever reused.
- **Swap.** Paper Positions pay swap at each rollover by the symbol's swap mode, three nights on the triple day.
- **Catch-up after downtime.** It pages bars from each position's last check: M1 where the M1 history reaches, then H1 and D1 for an older gap. Each call is bounded so it stays inside the broker timeout.

## Considered Options

- Reject Signals outside market hours, as before. Rejected: every GOLD Reopen and many GOLD Trend Signals would be lost, and live trading would drift from the evidence.
- Retry pending entries only at bar closes. Rejected: the Reopen fill window is shorter than its bar.
- Size Paper from the real Account (ADR 0007 as first written). Rejected: on a $50 Account, Paper could not show whether a strategy works at a sensible size.
