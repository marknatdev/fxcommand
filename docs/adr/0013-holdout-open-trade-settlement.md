# Settle open Holdout trades at final bar close with full transaction costs

When running a Holdout evaluation on out-of-sample data (2025-01-01 to present), a simulated position may remain open when the dataset ends.

Previously, handling of open trades at the end of the Holdout window was unspecified, risking uncounted open risk or disparate R accounting.

We resolve CCC-79 by adopting the following rule:
- Any position remaining open on the final bar of the Holdout data is settled at the close price of that final bar.
- The closing trade is charged full exit transaction costs: the symbol's Spread Floor plus 1 point of slippage.

This ensures every Holdout Trial produces closed, deterministic trade metrics with honest cost deductions.

## Considered Options

- **Discard open trades from metrics:** understates exposure and distorts win rate and profit factor by ignoring unfinished trades.
- **Mark-to-market without exit costs:** ignores realistic exit friction (spread and slippage) that would occur upon position liquidation.
- **Forced close at last bar close with full exit costs (chosen):** reflects realistic terminal liquidation cost and guarantees clean, reproducible closing statistics.
