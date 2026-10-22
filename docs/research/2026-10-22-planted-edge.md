# Planted edge calibration (M2 task T2)

Date: 2026-10-22. Code: `src/vibedesk/market/synthetic.py`. Reproduce: `tests/test_planted_edge.py`.

## Result

```python
PLANTED_EDGE = SyntheticConfig(reveal=2.0, follow=0.6, follow_hours=3)
PLANTED_EDGE_SEEDS = (1, 2, 3, 4, 5)
PLANTED_EDGE_RANGE = (date(2014, 1, 1), date(2026, 10, 1))
```

T2's acceptance holds on all five seeds:

| Criterion | Needed | Worst seed |
|---|---|---|
| Edge off, profit factor 2014–2024 | < 1.05 | 0.77 |
| Edge on, profit factor 2014–2024 | ≥ 1.3 | 1.47 (seed 4) |
| Edge on, profit factor 2025–2026 | ≥ 1.3 | 1.57 (seed 4) |
| Random sides at the same bars, profit factor 2014–2024 | < 1.05 | 0.93 (the highest of 20 runs × 5 seeds) |

With the edge off, every seed reproduces the bars from before T2 exactly. `tests/test_synthetic.py` pins their sha256.

**One deliberate change to the brief:** the hidden daily directions are balanced within blocks of 20 server days, not drawn independently. The next section explains why.

## Balanced hidden directions instead of independent coins

The brief said to draw an independent direction each server day. With independent coins, the edge adds a second random walk to the price level. At the calibrated strength, that walk is about as large as the base walk: the edge's own daily swing is about 1.0%, against the base walk's 1.2%.

The Spread Floor is fixed in points (55 on GOLD). The cost of a trade in R therefore scales with 1 / price. A seed whose price the edge dragged down paid several times the cost of the others:

- Seed 3 with the edge off trades between 1014 and 1767 (yearly means). With independent coins at reveal 1.5, follow 0.6, follow_hours 3, it fell to 412.
- Its 2025–2026 profit factor stayed between 1.08 and 1.20 however strong the edge was made (follow 0.6 to 0.8, reveal 1.5 to 2.0). A stronger edge also pushed the price lower.
- So with independent coins, no strength met "PF ≥ 1.3 on each of 5 seeds in 2025–2026" without far overshooting on the cheaper seeds.

Balancing fixes this. Each block of 20 server days has 10 ups and 10 downs in random order. The edge's effect on the price level then returns to zero at the end of every block. Each day's direction is still a fair coin on its own, so a random entry still has nothing to share.

| Seed | Edge off, yearly mean price | Independent coins (1.5 / 0.6 / 3) | Balanced (1.5 / 0.6 / 3) |
|---|---|---|---|
| 1 | 1158–3044 | 1289–4403 | 1118–2753 |
| 2 | 1212–2181 | 997–2276 | 1180–2081 |
| 3 | 1014–1767 | 412–1283 | 929–1755 |
| 4 | 722–1152 | 544–1080 | 733–1162 |
| 5 | 1141–2135 | 1213–1685 | 1169–2313 |

**The leak this costs.** Two days in the same block are slightly anti-correlated, at −1 / 19 ≈ −0.05. Over seeds 1–10 at the final values, the lag-1 autocorrelation of daily returns moves from −0.004 (edge off) to −0.031 (edge on). That is close to the prediction of about −0.02: −0.05 scaled by the edge's share of daily variance, about 0.4. No Block in the library reads the previous days' hidden directions, so this matters only to strategies tested in the planted-edge world, and only weakly. The random-walk known answer runs with the edge off and is unaffected.

## The mechanism

On each server day, a hidden direction d = ±1 is drawn from a stream of its own, `[seed, crc32(symbol), 1]`. With the edge off, that stream is never touched. With it on, no other draw moves: missing minutes, spreads and spikes are identical, and only prices change.

Each minute's return gets an added drift of d × strength × σ / √60, where σ is that minute's volatility. Each hour therefore drifts by its strength in hourly volatilities:

- **08:00–09:00 London time:** strength `reveal` (2.0). This is the opening hour, which is also the known-answer Spec's range.
- **The next `follow_hours` (3) hours:** strength `follow` (0.6) per hour.
- **Everywhere else:** zero. The data is the plain random walk.

London local time places the window, the same way `range_breakout(session="london_open")` does. All four hours fall in the generator's server-hour volatility band 10:00–15:00, in every week of the year.

The edge is strong by design: a known answer must be findable. Over its four hours, the day's hidden direction moves the price by about 3.8 hourly volatilities, while the noise over the same window is about 2.

## Method

**Known-answer Spec:** `range_breakout(session="london_open", range_bars=4)`, `atr_stop(mult=1.5)`, `r_target(r=2.0)`, direction both, GOLD M15.

**Cost:** `CostSnapshot(point=0.01, spread_floor_points=55, slippage_points=1)`, no swap. The 2× spread column uses `COST.stressed()`.

**Data:** one `SyntheticBarSource` per seed over `PLANTED_EDGE_RANGE`. The Spec is compiled and simulated once over all of it, with M1 settling, and trades are split by entry time:
- 2014–2024 is the Research Window;
- 2017–2024 is the walk-forward's blind years;
- from 2025-01-01 to 2026-09-30 is the Holdout.

Profit factors come from `vibedesk.engine.metrics.metrics` on each subset, after every cost.

**Random entries:** the Spec's own signal bars (long or short), each given a random side, with the same stop and target distances and the same Cost Model. That gives the same hours, the same count and the same exits as the Spec. There are 20 runs per seed, seeded `[seed, 99]`, and the table shows their mean and maximum.

**Search:** a coarse grid on seeds 1–2, then finer grids on seeds 1–5:
- reveal 0.5–2.0, follow 0.15–1.0, follow_hours 2–6;
- first with independent coins, then with balanced blocks.

The rule was the weakest setting that clears T2's acceptance on all five seeds with margin in both windows, and keeps the 2× spread profit factor at about 1.15 or more on at least 4 of 5 seeds, which mirrors T10's 4-of-5 rule. Strength put into `reveal` lifts the Spec without lifting random entries; strength put into `follow` lifts both. So the choice leans on `reveal`.

Balanced blocks, seeds 1–5, worst seed per column. The PF columns are 2014–2024 unless marked; "2×" is the 2× spread profit factor.

| reveal / follow / hours | PF min | PF mean | 2025–26 min | 2× min (4 best seeds) | Random-side max |
|---|---|---|---|---|---|
| 1.5 / 0.5 / 3 | 1.22 | 1.33 | 1.17 | 0.91 | 0.90 |
| 1.5 / 0.6 / 3 | 1.38 | 1.53 | 1.34 | 1.06 | 0.93 |
| 1.5 / 0.7 / 3 | 1.58 | 1.82 | 1.53 | 1.28 | 0.99 |
| **2.0 / 0.6 / 3** | **1.47** | **1.67** | **1.57** | **1.17** | **0.93** |
| 2.0 / 0.7 / 3 | 1.74 | 2.00 | 1.76 | 1.41 | 1.00 |
| 1.5 / 0.9 / 2 | 1.64 | 1.85 | 1.57 | 1.32 | 0.98 |

The edge off gives 0.65–0.77 over 2014–2024 on these seeds.

## Calibration table

Output of `uv run pytest tests/test_planted_edge.py -m slow -s` (55 s):

```
seed | walk PF: 2014-24 2025-26 | edge PF: 2014-24 2025-26 | 2x spread: 2014-24 2017-24 2025-26 | coin PF mean/max: 2014-24 2025-26
   1 |             0.76    0.83 |             1.72    1.90 |               1.29    1.30    1.70 |                    0.88/0.93 0.93/1.02
   2 |             0.76    0.88 |             1.83    1.98 |               1.36    1.38    1.47 |                    0.90/0.93 0.97/1.12
   3 |             0.75    0.69 |             1.65    2.16 |               1.17    1.23    1.47 |                    0.87/0.89 0.90/1.03
   4 |             0.65    0.66 |             1.47    1.57 |               0.86    0.80    0.91 |                    0.77/0.82 0.76/0.85
   5 |             0.77    0.79 |             1.68    1.81 |               1.28    1.29    1.39 |                    0.85/0.90 0.87/1.01

edge PF by entry year (mean cost in R)
seed | 2014 2015 2016 2017 2018 2019 2020 2021 2022 2023 2024 2025 2026
   1 | 1.83 (0.16) 1.60 (0.14) 1.76 (0.21) 1.51 (0.15) 2.02 (0.15) 1.63 (0.15) 1.66 (0.21) 1.61 (0.19) 1.86 (0.18) 1.48 (0.21) 2.10 (0.11) 1.89 (0.09) 1.91 (0.08)
   2 | 1.83 (0.17) 1.71 (0.17) 1.77 (0.10) 2.06 (0.13) 1.95 (0.15) 1.83 (0.16) 1.59 (0.19) 2.12 (0.16) 1.86 (0.20) 1.85 (0.16) 1.65 (0.17) 2.00 (0.15) 1.95 (0.18)
   3 | 1.36 (0.26) 1.68 (0.21) 1.60 (0.16) 1.59 (0.14) 1.65 (0.21) 1.58 (0.17) 1.91 (0.18) 1.76 (0.20) 1.53 (0.16) 1.75 (0.14) 1.76 (0.15) 2.46 (0.17) 1.84 (0.24)
   4 | 1.40 (0.22) 1.77 (0.21) 1.52 (0.19) 1.83 (0.23) 1.47 (0.23) 1.11 (0.32) 1.29 (0.28) 1.64 (0.21) 1.30 (0.23) 1.59 (0.31) 1.40 (0.29) 1.35 (0.29) 1.91 (0.25)
   5 | 1.39 (0.18) 1.76 (0.15) 1.92 (0.16) 1.87 (0.20) 1.65 (0.20) 2.17 (0.18) 1.39 (0.21) 1.70 (0.15) 1.50 (0.13) 1.72 (0.13) 1.56 (0.10) 1.78 (0.16) 1.85 (0.12)
```

The Spec takes about 325 trades a year: about 3,580 over 2014–2024 and about 560 in the Holdout. Every year of every seed is profitable: the lowest yearly profit factor is 1.11 (seed 4, 2019). The spread across years comes mostly from cost. A year whose mean cost is 0.3 R has a profit factor near 1.3; a year at 0.1 R is near 2.

The random-side column stays below 1.05 over 2014–2024 on every run. In the Holdout, where runs have fewer trades, single runs reach 1.12, but every seed's mean stays below 1.0.

## Fresh seeds

Seeds 1–5 shaped the parameter choice. Seeds 6–25 were run once afterwards, at the final values (`uv run python tests/test_planted_edge.py --seeds 6-25`):

```
seed | walk PF: 2014-24 2025-26 | edge PF: 2014-24 2025-26 | 2x spread: 2014-24 2017-24 2025-26 | coin PF mean/max: 2014-24 2025-26
   6 |             0.77    0.81 |             1.77    1.56 |               1.33    1.44    1.28 |                    0.89/0.93 0.86/0.97
   7 |             0.70    0.81 |             1.63    1.58 |               0.99    1.12    1.02 |                    0.83/0.89 0.87/1.01
   8 |             0.84    0.91 |             2.11    1.80 |               1.84    1.94    1.57 |                    1.01/1.06 0.96/1.12
   9 |             0.76    0.91 |             1.65    1.91 |               1.24    1.26    1.55 |                    0.88/0.95 0.93/1.04
  10 |             0.73    0.70 |             1.73    2.15 |               1.24    1.21    1.57 |                    0.86/0.92 0.99/1.13
  11 |             0.69    0.66 |             1.58    1.57 |               1.07    1.09    0.95 |                    0.86/0.90 0.84/0.95
  12 |             0.62    0.63 |             1.50    1.50 |               0.80    0.80    0.74 |                    0.78/0.85 0.82/0.95
  13 |             0.41    0.24 |             0.93    0.44 |               0.26    0.13    0.01 |                    0.51/0.54 0.26/0.31
  14 |             0.50    0.22 |             1.07    0.36 |               0.38    0.25    0.01 |                    0.59/0.65 0.24/0.28
  15 |             0.58    0.54 |             1.32    1.43 |               0.60    0.52    0.65 |                    0.70/0.74 0.76/0.85
  16 |             0.80    0.93 |             1.86    1.98 |               1.50    1.56    1.66 |                    0.92/0.98 0.95/1.02
  17 |             0.72    0.87 |             1.62    1.79 |               1.08    1.07    1.31 |                    0.87/0.93 0.87/1.02
  18 |             0.69    0.68 |             1.55    1.83 |               0.93    0.85    1.23 |                    0.80/0.85 0.87/0.93
  19 |             0.61    0.44 |             1.52    1.26 |               0.90    0.80    0.44 |                    0.80/0.84 0.64/0.74
  20 |             0.50    0.06 |             1.13    0.16 |               0.42    0.32    0.00 |                    0.62/0.64 0.10/0.12
  21 |             0.72    0.70 |             1.69    1.49 |               1.21    1.24    1.02 |                    0.86/0.91 0.78/0.88
  22 |             0.54    0.40 |             1.14    0.77 |               0.36    0.23    0.11 |                    0.61/0.66 0.41/0.50
  23 |             0.72    0.79 |             1.71    1.94 |               1.26    1.25    1.54 |                    0.89/0.94 0.93/1.04
  24 |             0.72    0.63 |             1.58    1.63 |               1.12    1.16    1.17 |                    0.82/0.88 0.86/1.03
  25 |             0.78    1.01 |             1.75    1.67 |               1.29    1.34    1.21 |                    0.88/0.95 0.84/0.96
```

How many of the 20 fresh seeds clear each bar:

| Bar | Seeds |
|---|---|
| Edge off, PF 2014–2024 < 1.05 | 20 of 20 (highest 0.84) |
| Edge on, PF ≥ 1.3 in both windows | 15 of 20 |
| Edge on, PF ≥ 1.05 at 2× spread over 2017–2024 | 12 of 20 |
| Both of the above | 12 of 20 |
| Every random-side run < 1.05 over 2014–2024 | 19 of 20 (seed 8 reaches 1.06) |

Every failure is a seed whose price wandered low. Seeds 13, 14, 20 and 22 pay 0.38–0.48 R per trade over 2014–2024, and up to 1.04 R in single years. The base random walk, not the edge, put them there: with the edge off, the same seeds have the lowest profit factors (0.41–0.54).

## Risks for the full Pass Bar (T10)

T10 must reach CANDIDATE in at least 4 of 5 seeds, through the real Pass Bar. Use `PLANTED_EDGE`, `PLANTED_EDGE_SEEDS` and `PLANTED_EDGE_RANGE` as they are. A different start date moves both the base path and the day blocks, and these numbers no longer apply.

**1. Spread stress leaves no spare (high).** Over the blind years 2017–2024, the profit factor at 2× spread is 1.30, 1.38, 1.23, 0.80 and 1.29 on seeds 1–5.
- Seed 4 fails at any strength that keeps the other seeds realistic. Its base price stays at 722–1152, so a trade costs about 0.25 R, or 0.5 R at 2× spread.
- That uses up the one failure T10 allows. Any other check that fails on any other seed breaks the 4-of-5 rule.
- On fresh seeds, only 12 of 20 pass this check. If each seed passed with that rate on its own, 4 of 5 would succeed only about one time in three.

The root cause is not the edge. ADR 0004 fixes the Spread Floor in points: 55, measured at today's GOLD price, which is roughly three times the 2014 level. Synthetic GOLD starts at 1,250, near the 2014 price, and random-walks with no upward drift. Its cost in R is therefore about three times today's real cost, and much more on seeds that wander low. Options, which are T10's or the owner's to choose:

- A Spread Floor scaled to the synthetic price for the known-answer test, for example 17 points (about 55 / 3). With that floor and the same edge, the 2× spread profit factor over 2017–2024 is 1.23–1.70 on seeds 1–5, so all five pass, and it passes on 15 of 20 fresh seeds. The random walk stays at or below 0.88 over 2014–2024 on all 25 seeds. The catch is that random-side profit factors rise too, up to a mean of 1.04 and a single run of 1.10 on seed 8. No Pass Bar check reads that, but it would break T2's own random-entry criterion if the edge were calibrated at that floor.
- An opt-in level anchor in the generator, such as weak mean reversion of log price toward the start price. It would change bars only when switched on, so edge-off reproduction would hold.
- A stronger edge. This is the weakest option: it overshoots on the cheap seeds, and it lifts random-side profit factors there above 1.05 (seed 8 is already at 1.06).

**2. PBO with a grid (medium).** If the known-answer Spec gets a grid whose combinations all share the edge equally (say several `r` values), in-sample rank says nothing about out-of-sample rank. PBO then tends to 0.5 and fails the ≤ 0.3 bar. Options:
- run a grid of one, which makes PBO "n/a";
- use a grid whose combinations really differ in how much of the edge they reach, such as `range_bars` 4 against 1 or 12, or `session` london_open against asia.

The same applies to the Prosecutor's neighbour check, which needs neighbours.

**3. The Holdout is short (low).** 2025-01-01 to 2026-09-30 holds about 560 trades. That puts about ±0.13 of noise on a profit factor near 1.6. The Holdout PASS bar is PF ≥ 1.1 with ≥ 30 trades, and the lowest Holdout profit factor on seeds 1–5 is 1.57, so the margin is wide.

**4. Checks that look safe on seeds 1–5:**
- **Walk-forward PF ≥ 1.2:** the lowest 2014–2024 profit factor is 1.47.
- **Fold consistency, ≥ 5 of 8 profitable folds:** every year of every seed has PF ≥ 1.11.
- **Coin-Flip Baseline:** random-side profit factor is ≤ 0.93 against the Spec's ≥ 1.47.
- **Deflated Sharpe:** there are about 2,600 blind trades.
- **Side split:** the edge is symmetric.
- **Top-5 share:** too many trades for five to matter.
- **ATR terciles:** measured as terciles of the stop distance, which is 1.5 × ATR(14) at the signal bar, the profit factors run 1.38–1.95 on seeds 1–5.

**5. Housekeeping:**
- The calibration test uses `@pytest.mark.slow` before T10 registers that marker, so pytest prints an unknown-marker warning.
- A `skipif` on the `-m` expression keeps the 55-second test out of the default run until T10's `addopts` lands. It still runs under `uv run pytest -m slow`.

## Bars with the edge on

**Structure:** times, missing minutes, spreads, spikes, weekends, the point grid and the M1-to-D1 aggregation are all unchanged. `tests/test_synthetic.py` runs every structure test on both the walk and the edge.

**Daily volatility:** close-to-close D1 volatility rises from 1.11–1.33% to 1.50–1.83% (seeds 1–10). Balancing doesn't change this: the extra variance comes within each day.

**Price level:** yearly mean prices track the edge-off path within 12% on seeds 1–10. The largest gap is seed 1, at 11.5%.

**Lag-1 autocorrelation of daily returns:** −0.004 edge off and −0.031 edge on, averaged over seeds 1–10 (see the leak note above).
