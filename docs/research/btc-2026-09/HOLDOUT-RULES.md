# BTCUSD sealed holdout: rules, fixed before scoring

Written 2026-09-27, before the holdout bars were read by any strategy.

- **Finalist (one):** `trend_breakout` on BTCUSD H4, entry=100, exit=50, atr_period=20, sl_atr=2.0, tp_atr=0, allow_short=false. It is ledger trial #6 of `results/ledger_round1.json`, pre-registered and chosen by the finalist bar below. No neighbour or other variant is scored.
- **Why it is the finalist:**
  - 94 trades, which is 30 or more.
  - Mean R is positive in every research period: +0.114, +1.059 and +2.069.
  - SQN is 1.34, above deflation(24 trials) = 1.26.
  - All 27 ±12% neighbours are positive overall. That is the app's rule, `research.evaluate_hypothesis`, checks `mean_r > 0`.
- **Window:** 2025-09-01 00:00 server time (`research.holdout_from`) to the last closed H4 bar in `backend/data/research/btc.pkl`. Research bars before the seal warm the indicators up.
- **What counts:**
  - Only trades whose Signal came at or after the seal. A position signalled before the seal is excluded.
  - A position still open at the last bar is closed at that close and counts. This is `backtest`'s "end" trade, as in `research.holdout_job`.
- **Pricing:** as in research.
  - Spread: 2× the greater of today's $40 scaled by price and the bar's recorded spread.
  - Slippage: $5, scaled by price.
  - Swap: the terminal's figures, charged for every server midnight.
- **Pass rule:** at least 5 trades and mean R above 0. There is no BTC Champion to beat. This is `research.HOLDOUT_RULE` without the Champion clause.
- **Scored once.** The result goes to `results/holdout_btc_2026-09-27.jsonl` and is final. When BTCUSD research moves into the app, this Candidate key's BTCUSD H4 holdout must be seeded into `research_trials` as spent.
