# Strategy research, September 2026

This is the offline research behind [spec-better-strategies.md](../../../spec-better-strategies.md). It uses price history and symbol specs read from an XM MetaTrader 5 terminal (read-only: `copy_rates_*`, `copy_ticks_range` and `symbol_info`, never an order function). Every trade is replayed through FXCommand's own `PaperTrader` (`backend/fxcommand/learning/paper.py`), so fills, stops and gaps follow the same rules as Shadow Trades.

The scripts were written as one-off research, not app code. They expect their working folder at `D:\fxcommand\backend\data\research`, which is gitignored. Copy them there, or change `S` in `lab.py` and the paths in each script. Then run `pull.py` (all symbols) or `pull_gold.py` (GOLD only) first. The history pickle is not committed; it is market data of up to ~100 MB.

## Scripts

| Script | What it does |
|---|---|
| `probe.py`, `pull.py`, `pull2.py` | Read-only: symbol specs, then up to 99k bars per symbol for M15/H1/H4/D1 |
| `screen.py` | Read-only: for every tradable symbol, the D1 ATR stop in $ at the minimum lot |
| `ticks.py` | Read-only: GOLD's median tick spread per 15-minute slot, last ~40 trading days |
| `lab.py` | Harness: loads history and runs signal frames through `PaperTrader` |
| `study.py` | Textbook families (old defaults, Turtle, EMA50/200, Connors RSI2, pullback, London breakout) across 21 symbols × 4 timeframes |
| `robust.py` | Swap per night by `swap_mode`; spread ×1/×2/×3; the 36-set neighbourhood for RSI2 on indices |
| `small.py` | RSI2 pullback on the indices that are cheap at the minimum lot, with the risk % that lot implies |
| `ruin.py` | Bootstrap of the US500 trade sequence at the minimum lot on $50 |
| `port.py` | Trend portfolios across 17 symbols, including swap |
| `gold.py` … `gold5.py`, `prop.py` | GOLD: hour-of-day drift, reopen entries, EMA/Turtle grids, previous-day breakout. `prop.py` scales spread, slippage and swap to each bar's price |
| `pull_gold.py` | Read-only: GOLD history and specs, including `swap_rollover3days` |
| `gold6.py` | Upgrade round on research data before 2025-09-25: Trend ensembles and regime filters; Reopen multi-timeframe, day and exit filters. Writes the trial ledger |
| `gold7.py` | Pyramiding simulation for GOLD Trend |
| `holdout.py` | One-time scoring of the finalists on the sealed year (2025-09-25 onwards) |
| `pull_scalp.py` | Read-only: GOLD M1/M5 by date chunks (capped by the terminal's 100k "Max bars in chart") |
| `pull_m15.py` | Read-only: GOLD M15 by date chunks after the bar cap was raised |
| `scalp2.py` | Scalping over 2020–2026 in three periods, including M1-trigger versions (`scalp2.py <spread> <slippage>`) |
| `scalp3.py` | Neighbourhood of the NY opening-range breakout (range 15–60 min, target 1.5–3×, M15 filter on/off) |
| `scalp1.py`, `scalp1_cost.py` | GOLD scalping, M15 context to M5 setup: 10 designs at XM Standard costs, zero cost and a low-cost account (`scalp1_cost.py <spread> <slippage>`). Sealed from 2026-06-25 |

## Results

- `study_*.csv`: one row per symbol × timeframe × family: trades, mean R, SQN, drawdown, and mean R in each half of the history.
- `surv.log`, `neigh.log`: survivors under 1×, 2× and 3× spread plus swap, and the RSI2 neighbourhood.
- `gold_asia.csv`: the GOLD reopen and Asian-session grid at fixed dollar costs. This version is superseded by the price-scaled run in `gold5.py`.
- `gold_prop.csv`: GOLD EMA and Turtle grids with price-scaled costs, by period (2010–17, 2018–21, 2022–26).
- `screen.csv`: minimum-lot stop sizes for every tradable symbol.
- `ledger_gold6.json`: the 18 upgrade trials, with per-period mean R.
- `pyramiding.jsonl`: pyramiding variants.
- `ledger_scalp1*.json`: scalping trials at XM costs, zero cost (`cost0`) and ~$0.20 round trip (`cost0.17`). All negative after costs.
- `ledger_scalp2_*.json`, `ledger_scalp3_xm.json`: full-history scalping at zero, low and XM costs, and the NY ORB neighbourhood (0 of 24 positive in every period).
- `holdout_2025-09-25.jsonl`: finalists on the sealed year. The dip filter and the 05:00 exit failed here; the baselines held.

## Caveats

- **Selection:** about 650 symbol × family × timeframe combinations and about 400 GOLD variants were tried. Pick by consistency across periods and neighbouring parameters, never by the highest backtest.
- **Costs:** a fixed-dollar spread and swap overstate the cost of early years. Use the price-scaled model (`prop.py`, and decision D34 in the spec).
- **Spread column:** the bar `spread` column understates the real spread (GOLD: bar median about $0.36 against $0.55 from ticks).
- **Account figures:** figures for a $50 account are hypothetical sizing arithmetic, not a record of any account.
