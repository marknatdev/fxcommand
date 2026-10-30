# Night Shift operates within strict Trial and token caps and cannot spend Holdout data

Night Shift is an autonomous background research loop that explores queued ideas and parameter mutations while the owner is offline.

To prevent runaway API token spend or unintended data contamination, Night Shift is bounded by strict constraints:
1. Max 20 Trials per night (configurable in Settings).
2. Max 50% of the daily token budget (configurable in Settings).
3. Every Trial is charged to the Ledger under its family budget.
4. AI may not request or spend Holdout data.
5. AI may not promote strategies into the Forward Arena.
6. Progress and early termination reasons are communicated via a 07:00 Asia/Bangkok Telegram morning brief.

## Considered Options

- **Unbounded autonomous exploration:** maximizes discovery rate, but risks exhausting the daily token cap and running excessive backtests that inflate the family combination count $N$.
- **Autonomous promotion to Arena:** enables full pipeline autonomy, but violates the owner decision that only human review can deploy strategies forward.
- **Capped Trial exploration with morning brief (chosen):** bounds financial costs and multiple testing penalties while providing productive autonomous exploration.
