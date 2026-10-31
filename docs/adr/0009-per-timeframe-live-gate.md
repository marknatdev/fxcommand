# Per-timeframe trade thresholds and statistical bounds for the Live Gate

The Live Gate evaluates whether forward paper performance in the Arena justifies risking live capital. It is computed and displayed for observational proof only; no orders are placed.

Rather than enforcing a flat trade count across all timeframes (such as 100 trades), the Live Gate scales required sample size by bar frequency:
- M15: $\ge 100$ forward closed trades
- H1: $\ge 60$ forward closed trades
- H4: $\ge 40$ forward closed trades
- D1: $\ge 30$ forward closed trades

In addition to sample size, the strategy must demonstrate that the 1-sided 95% bootstrap lower confidence bound of its forward Profit Factor exceeds 1.00 ($\text{PF}_{\text{lower}} > 1.00$), and that maximum equity drawdown remains below 6.0%.

## Considered Options

- **Flat 100-trade requirement:** statistically uniform, but practically eliminates H4 and D1 strategies, which would require 1 to 3 years of forward paper trading before qualifying.
- **Unadjusted forward Profit Factor > 1.0:** vulnerable to sample noise; a 15-trade lucky run on H4 could pass despite high statistical uncertainty.
- **Per-timeframe trade scaling with statistical lower bound (chosen):** balances time-to-decision across timeframes while ensuring positive mathematical expectancy with 95% statistical confidence.
