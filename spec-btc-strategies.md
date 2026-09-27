# Spec: BTC Trend on BTCUSD H4

Status: approved design, 2026-09-27. It follows `spec-better-strategies.md` (the GOLD spec, D1–D56). Decision IDs here are D1–D18 of this spec. "GOLD D24" means decision D24 of the GOLD spec.

Captured at commit `f5e2fab` (main, after PR #3).

## Overview

The operator asked for a BTCUSD strategy designed the same way as the GOLD pair: measure first, pick only what survives costs out of sample, and run it on Paper before any real money.

The research ran 24 pre-registered trials in five families on the operator's XM history, 2018-01 to 2025-08. One finalist survived: **BTC Trend**, the existing `trend_breakout` Strategy with a 100-bar entry channel, a 50-bar exit channel, a 2×ATR(20) stop, long-only, on H4.

- It passed the sealed holdout (2025-09-01 to 2026-09-27) under rules written before scoring.
- The pass rests on one position that is still open. The closed holdout trades lost.
- It therefore ships **Paper-only**, and the scorecard decides later (D10).

Most of the work is not the strategy, which already exists. It is making the app price and judge a symbol that differs from GOLD:

- BTCUSD trades 24/7.
- It pays swap every night.
- Its early intraday history is fake.
- Its past spreads were far wider than today's scaled figure.

## Goals and Non-Goals

### Goals

- Run BTC Trend as a Paper Session on BTCUSD H4, 7 days a week.
- Price BTCUSD honestly everywhere the app prices it: Backtests, Evidence, Shadow Trades, Optimizer Runs, the Cost Check swap display and research. That means:
  - swap charged every night;
  - each bar's recorded spread as a floor;
  - only trusted history.
- Record the BTC research in the app's trial ledger, including the spent holdout, so the weekly review cannot take a second look.
- Add BTCUSD H4 to the weekly Strategy Review.

### Non-Goals

- Live trading BTC. Going live remains the operator's decision, from the scorecard (GOLD D29).
- Any intraday BTC strategy. All 16 intraday and H1 trials lost after costs.
- Shorts on BTC. Every short variant lost.
- Changing GOLD's cost pricing. That is an open question for the GOLD review (D14).
- Generating weekends in SimBroker. The sim clock stays Monday–Friday so pinned inputs don't move (D16).

## Research Summary

All figures below are from the operator's XM terminal, read-only. They were produced with FXCommand's own `PaperTrader` fill rules through the scripts in `docs/research/btc-2026-09/`.

**Market facts (BTCUSD on XM, 2026-09-27):**

| Fact | Value |
|---|---|
| Hours | 24/7. Weekend bars exist from 2022; before that, a weekly gap. No daily break |
| Spread | Fixed $40 on weekdays, at midnight and on weekends. About 4.7 bp at $84k. No reopen effect |
| Past spreads | Recorded bar spread about $126 in 2019–21 at $4k–$60k; $30–50 in 2022–23; about $90 in 2024. Ticks exist only from 2024, where they agree with the bar column ($90 vs $86) |
| Swap | Points mode: long −3500.13, short −2333.42, so −$35 / −$23 per lot per night at today's price. Both sides pay. `swap_rollover3days = 7`, which is outside MT5's 0–6 day enum. Third-party pages claim XM crypto has no swap; the terminal is the source used here |
| Contract | 1 BTC per lot, minimum 0.01, step 0.01, stops level 0 |
| History | D1/H4/H1/M15 from 2013, but 2013–2015 intraday bars are daily bars copied down (M15, H1 and H4 ATR% equal D1's). Real intraday data starts in 2016–17. M5 reaches only 2022-12 (400k cap) |

**Which timeframes can work** (today's price, median ATR over the last 12 months, 2×ATR stop):

| TF | Stop | Cost R at 1× / 2× spread | Min-lot risk | Swap per night, long |
|---|---|---|---|---|
| M15 | $446 | 0.10 / 0.19 | $4.5 | 0.08R |
| H1 | $1,006 | 0.045 / 0.09 | $10 | 0.035R |
| H4 | $2,054 | 0.022 / 0.044 | $20.5 | 0.017R |
| D1 | $5,472 | 0.008 / 0.016 | $55 | 0.006R |

**Round 1** (`results/ledger_round1.json`, 24 trials):

- **Research window:** Signals from 2018-01-01 to 2025-08-31. Periods are 2018-01..2020-06, 2020-07..2022-12 and 2023-01..2025-08.
- **Pricing:**
  - spread = 2 × max(today's $40 scaled by price, the bar's recorded spread);
  - slippage = $5, scaled;
  - swap = the terminal's figures, scaled, for every server midnight held.

| Family | Result |
|---|---|
| 1. Turtle breakout (H4, H1; long and short) | H4 100/50 long **+1.07R** (+0.11 / +1.06 / +2.07). H4 55/20 long +0.19R, but −0.70 in 2018–20. H4 20/10 long +0.12R. H1 long −0.75R. Every short negative |
| 2. Time of week (weekend hold; US and Asia sessions) | −0.66 to −0.90R, both directions. Closing before the rollover avoids swap, but the spread eats the move |
| 3. US-open range breakout (H1) | −0.85 to −0.98R |
| 4. Trend pullback (H4, EMA200 + RSI) | −0.27 to −0.46R |
| 5. Midday momentum, flat before the rollover (H1) | −0.38 to −0.67R |

**The finalist: Turtle 100/50 long on H4.**

- The app's `trend_breakout` reproduces it trade for trade (`scripts/parity.py`).
- 94 trades, about 12 a year; SQN 1.34, above deflation(24) = 1.26.
- **Neighbours:** all 27 of its ±12% neighbours (entry, exit, stop) are positive overall, which is the app's rule. Only 10 of the 27 are positive in every period, and the median 2018–20 result across the grid is negative. It is a plateau overall, and fragile early.
- **Tail dependence:** median trade −1.0R; the best 5 trades are 142% of the total; 2023 alone is about 45%.
- **Cost sensitivity:** +2.9R at 1× spread, +1.07R at 2×, −0.41R at 4×. Without swap, +1.21R.

**Holdout** (`HOLDOUT-RULES.md`, written before scoring; `results/holdout_btc_2026-09-27.jsonl`):

- 2025-09-01 to 2026-09-27: 11 trades, **+0.47R each: passed** (at least 5 trades, mean R above 0).
- The pass comes entirely from one position still open at the end, marked at +12.0R. The 10 closed trades average −0.69R: 1 winner, 9 stopped out.
- The rule counts the open position, as `research.holdout_job` does. Accepting the pass was decided under D10.

**GOLD cross-check (diagnostic, not a trial).** A recorded-spread floor at 1× would turn GOLD Trend's 2010–17 period from +0.12R to −0.03R, and GOLD Reopen's from +0.03R to −0.02R. GOLD Reopen is already slightly negative at the Evidence's 2× scaled spread (−0.015R). See D14.

## Technical Design

### Symbol Cost Profile (new, `learning/costs.py`)

Settings a symbol's pricing needs beyond `SymbolInfo`. It is a code constant keyed by the broker's symbol name, and GOLD and FX get the default profile:

```python
@dataclass(frozen=True)
class SymbolCostProfile:
    history_from: int | None = None   # Trusted History start (server time); None = RESEARCH_START
    bar_spread_floor: bool = False    # price each bar at max(scaled typical spread, the bar's recorded spread)

SYMBOL_PROFILES = {"BTCUSD": SymbolCostProfile(history_from=<2018-01-01>, bar_spread_floor=True)}
def profile(symbol: str) -> SymbolCostProfile
```

- The profile is pinned by a test.
- A review PR may not change it: the gold-review skill forbids editing cost rules.
- `COST_MODEL_VERSION` goes to 2, which invalidates every Evidence key. That is intended: FX and GOLD swap is unchanged, but the version is shared.

### Swap every night

- **The problem.** `CostModel.from_symbol` maps `swap_rollover3days` through `mt5_day_to_weekday`. The value 7 (outside 0–6) became Sunday, and `rollover_nights` counts only Monday–Friday, so BTC swap was undercounted.
- **The field.** A new `CostModel.swap_every_night: bool`.
  - `from_symbol` sets it when `swap_rollover3days` is not in 0–6. Otherwise it keeps `triple_weekday` as today.
- **The count.** `rollover_nights(open_ts, close_ts, triple_weekday, every_night=False)`: with `every_night`, one night per server midnight in (open, close], 7 days a week, no triple day.
- **Where the change reaches.** Every caller goes through `CostModel.swap_for`, or through `rollover_nights` with the model's flag:
  - Backtests;
  - Evidence `avg_nights`;
  - Shadow Trades;
  - Optimizer Runs;
  - the Cost Check's swap display, via `expected_hold`;
  - research.
- **Worst case assumed (D6).** If the operator's statement ever shows no weekend swap, the fix is a profile flag. It is not a guess made now.

### Recorded-spread floor

- **The multiplier.** A new `CostModel.bar_floor: float = 0.0`: a multiplier on the bar's recorded spread, where 0 means off.
  - `backtest()` and `PaperTrader.on_bar` pass the bar's `spread × point` to the costs, and `spread_at(price)` returns `max(spread × scale(price), recorded × bar_floor)`.
- **Who sets it.**
  - Evidence Runs, research backtests and holdout evaluations set `bar_floor = SPREAD_MULTIPLIER` (2).
  - Shadow Trades and Optimizer Runs set it to 1.
- **Scope.** Only symbols whose profile has `bar_spread_floor` get a floor; for every other symbol `bar_floor` stays 0 (D14).
- **Bars without spread.** A bar whose recorded spread is 0 (BTC before 2017, and sim bars without a spread column) falls back to scaling.
- **Live Cost Check unchanged.** It prices from `SpreadBook` today (typical spread), and no floor applies to it.

### Trusted History

- **`research.trusted_from(symbol)`** returns `max(RESEARCH_START, profile.history_from)`. It is the only definition.
- **Evidence Runs.** Trades signalled before it don't count; bars before it only warm up indicators.
  - `evidence_job` gets `first_ts = trusted_from`, so BTC `periods()` falls back to thirds of 2018 → now.
  - `EvidenceSpec.key` gains `h<trusted_from>` and the swap-night mode (`n7` for every night, `n5t<day>` for Monday–Friday with a triple day), so a changed start or swap rule cannot show stale Evidence.
- **Optimizer Runs** read the last `learning_bars` (5000) bars. For BTC H4 that is about 2.3 years, all trusted; `trusted_from` also cuts them, for when that setting grows.
- **Research.**
  - `evaluate_hypothesis` and `record_trial` use `trusted_from` instead of `RESEARCH_START`.
  - Snapshot exports keep the older bars, marked as warm-up.
  - The finalist bar is unchanged.

### Strategy identity (D11)

- `TREND_BREAKOUT.title` changes to "Trend Breakout", with a description that names GOLD H4 and BTCUSD H4.
- The key `trend_breakout`, its params and `family="gold"` are unchanged. The family is what the reviewer's Challenger rule keys on.
- `frontend/src/lib/format.ts` gets the same label.
- The golden fixtures don't change, because signals are unchanged.

### Seeded ledger and spent holdout (D12)

- **The seed.** An idempotent startup seed (`learning/repo.seed_research(store)`, keyed by a seed id) writes the BTC research into `research_trials`:
  - the 24 round-1 trials, split by Arena: 8 on BTCUSD H4, 16 on BTCUSD H1. Each has `source="seed"`, the hypothesis name, `data_from`/`data_to` and the result row;
  - one holdout row for BTCUSD H4 with the `trend_breakout` Candidate key (entry 100, exit 50, atr 20, sl 2.0, tp 0, long-only), `holdout_used=True`, `status="passed"` and the holdout result.
- **The effect.**
  - `evaluate_holdout` finds the row and refuses a second holdout on that key ("already scored").
  - The trial count feeds the selection penalty.
- **The data.** It lives in a checked-in JSON file (`backend/fxcommand/learning/seeds/btc-2026-09.json`), copied from the research results. It holds research figures only, no account data.

### SimBroker (D16)

- **The symbol.** `BTCUSD` is added at the end of `DEFAULT_SYMBOLS`, never to `LEGACY_SYMBOLS`:
  - price about 84,000, digits 2, point 0.01, contract size 1;
  - volume min/step 0.01;
  - fixed spread 4000 points;
  - swap points −3500.13 / −2333.42, `swap_rollover3days = 7`;
  - no `open_minutes` (no daily break).
- **The clock** still skips weekends for every symbol.
- **Weekend behaviour** is covered by unit tests at explicit Saturday and Sunday timestamps (costs, window, `_first_tradable` with a fresh quote), not by the sim clock.
- **Pinned inputs.** Adding the symbol must not move the pinned inputs: the golden fixtures and E2E figures must pass unchanged. If the sim's random draws are shared across symbols, BTCUSD gets its own seeded stream.

### Session setup (D12, D13)

- **Who creates it.** The operator creates the Session from the dashboard, as with GOLD. Claude doesn't start Sessions. The Session:
  - is named "BTC Trend", with Paper execution;
  - has one Assignment: BTCUSD H4, `trend_breakout` with entry 100, exit 50, atr_period 20, sl_atr 2.0, tp_atr 0, long-only;
  - keeps its Trading Window **disabled** ("always open"), with Weekend Close off.
- **The editor.**
  - It already supports a disabled Trading Window.
  - It shows a hint when a 24/7 symbol is paired with a Monday–Friday window: "this symbol trades every day; the window skips weekends".
  - A symbol counts as 24/7 when its profile says so or its recent bars include weekends.
- **Learning.** Auto-promotion stays off for this Arena (D19). A Challenger that looks better is the operator's call, after its own research and holdout.
- **Sizing.** The Paper Account sizes it, and Paper limits apply (ADR 0008).
- **Min-lot risk.** At today's stop, one minimum lot risks about $20. That is a large share of a small real account, and the scorecard shows it.
- **Engine.** Nothing in the engine assumes weekends are shut:
  - `_first_tradable` and "still shut" go by quote freshness;
  - Pending Entries go by the observed Next Tradable Time.
  - A test pins this.

### Weekly Strategy Review (D13)

- **The skill.** `.claude/skills/gold-review/SKILL.md` gains a **BTCUSD H4** Arena:
  - Champion: BTC Trend.
  - Budget: about 5 backtests a week.
  - Its holdout for the Champion key is spent.
  - No intraday BTC research unless a hypothesis names the cost it beats.
- **Unchanged.** The skill name, the scheduled task and the allow-list stay the same, and no MCP tool is added. The allow-list's research pattern (`uv run --directory backend fxcommand-research:*`) names no symbol, so BTC research is already allowed; `tests/test_research_cli.py` keeps pinning the allow-list.
- **Added to the skill.** The no-cost-edits rule (see Security Considerations), and the rule that BTC auto-promotion stays off (D19).
- **The review's first BTC job:** check the scorecard and the Paper record. It never promotes.

## Error Handling and Edge Cases

- **The terminal reports a different `swap_rollover3days` later** (for example 3). `from_symbol` goes back to a triple day. `EvidenceSpec.key` carries the swap-night mode (`n7`, or `n5t<day>`), so Evidence priced under the old mode shows as "other settings", never as current.
- **Swap turns out to be free, or weekday-only.** The operator reads it from a real statement. The fix is a one-line profile change plus a version bump. Until then the worst case stands.
- **A bar with a recorded spread of 0** falls back to scaling. The history before `trusted_from` never counts anyway.
- **Weekend maintenance gaps** (XM pauses crypto briefly on some weekends). Quotes go stale, so the engine waits, as it does for GOLD's daily break. A Pending Entry expires by `fill_limit` as usual.
- **The position that carried the holdout.** It exists only in the backtest. The Paper Session starts flat and waits for the next 100-bar breakout.
- **Scorecard patience.** At about 12 trades a year, `MIN_SCORECARD_TRADES = 10` means "too few trades" for about a year. The band will be wide, given the tail.
- **GOLD Evidence after the version bump.** Every Evidence key changes, so the badges read "stale" until the operator re-runs Evidence. The engine never starts Evidence Runs by itself for this.

## Security Considerations

- **Read-only on MT5.** Every probe and research script reads only: `symbol_info`, rates and ticks. The research scripts never call order methods, and the MT5 smoke test adds only read-only BTCUSD checks.
- **What goes in the repo.** The repo is public, so the spec, the seed file and the research results hold backtest figures only: no account number, balance, equity or P&L.
- **What the review can't do.** Today's skill does not forbid cost edits: only `.claude/**` and `.mcp.json` edits are denied. Step 10 adds the rule "never change `SYMBOL_PROFILES`, `COST_MODEL_VERSION`, cost rules or `learning/seeds/`" to the skill, and a test pins the profile map and seed contents so that a review PR changing them fails CI.

## Testing Strategy

- **Costs** (`test_costs.py`):
  - `rollover_nights(..., every_night=True)` counts Saturday and Sunday midnights, with no triple day;
  - `from_symbol` with `swap_rollover3days=7` sets `swap_every_night`, and with 3 keeps Wednesday triple;
  - BTC swap for a 9-night hold equals 9 × the per-night swap, scaled;
  - `spread_at` with `bar_floor`;
  - GOLD's profile has no floor.
- **Backtest:** with a floor, a bar whose recorded spread exceeds the scaled spread fills at the recorded one; with `bar_floor = 0`, results equal today's.
- **Evidence:**
  - the key contains `h<trusted_from>`, the swap-night mode and cost version 2;
  - a BTC Evidence Run ignores trades before 2018, and its periods are thirds;
  - GOLD Evidence is unchanged except for the version.
- **Research:**
  - `trusted_from("BTCUSD")` is 2018-01-01 and `trusted_from("GOLD")` is `RESEARCH_START`;
  - the seed is idempotent;
  - `evaluate_holdout` on the seeded key refuses;
  - the trial count for BTCUSD H4 is 9 after seeding (8 trials plus the holdout row), and 16 for BTCUSD H1.
- **Engine:**
  - a BTC Paper Session with the window disabled evaluates and fills at a Saturday timestamp, using the sim with an explicit clock and a fresh quote;
  - the Cost Check shows BTC swap from 7-night counting.
- **Strategy:** the golden fixtures pass unchanged, and the title reads "Trend Breakout".
- **Parity on real data** (read-only script, `docs/research/btc-2026-09/scripts/crosscheck_app.py`): the app's `evidence_job` path, with profile, floor and every-night swap on `btc.pkl`, reproduces ledger #6 (94 trades, +1.07R) within rounding.
- **E2E:** BTCUSD appears in the editor, and a BTC Assignment with the window disabled saves. The 24/7 hint appears with the default window.
- **MT5 smoke (read-only):** BTCUSD `symbol_info` builds a `CostModel` with `swap_every_night`.

## Decisions Log

| ID | Topic | Decision | Rationale | Source | Date |
|---|---|---|---|---|---|
| D1 | What to design | A research-first BTC strategy spec; the review extension follows once a BTC Arena has an Assignment | The GOLD spec held because its strategies came from measurements | Interview | 2026-09-27 |
| D2 | Timeframes | H1 and H4 only | M15 costs 0.19R per trade at Evidence pricing, plus 0.08R of swap per night; D1 min-lot risk is too big for a small account | Interview | 2026-09-27 |
| D3 | Weekends | Trade 7 days a week: Trading Window disabled, Weekend Close off for BTC | No weekend gap to protect against; cutting weekends drops about 2/7 of signals | Interview | 2026-09-27 |
| D4 | Direction | Research decides; test both | BTC has deep bear years and shorts pay less swap. Result: every short lost, so BTC Trend is long-only | Interview | 2026-09-27 |
| D5 | Research window and pricing | Signals from 2018; each bar priced at 2 × max(today's spread scaled, the bar's recorded spread) | Price scaling alone makes 2019–21 look 3–10× cheaper than it was; 2013–16 intraday bars are fake | Interview | 2026-09-27 |
| D6 | Swap | Terminal figures, every night (worst case) until a real statement shows otherwise | A strategy that needs free swap is not worth trading | Interview | 2026-09-27 |
| D7 | Method | Read-only research scripts in `docs/research/btc-2026-09/` before the spec; holdout sealed from `holdout_from(now, tf)` = 2025-09-01 | The app was not running, and `fxcommand-research` did not price BTC correctly yet | Interview | 2026-09-27 |
| D8 | Families | Five pre-registered families (Turtle, time of week, US-open breakout, trend pullback, intraday momentum), up to about 6 variants each, all counted | Families 2 and 5 test the swap cost; the rest are the known candidates | Interview | 2026-09-27 |
| D9 | Nothing passes | Record the result; ship only the swap-count fix | That fix is a real bug for any symbol with an out-of-range triple day | Interview | 2026-09-27 |
| D10 | Accepting the holdout pass | Accept the pass (rule fixed beforehand, counts the open position as `holdout_job` does) and run **Paper-only**; no live until the Paper record sits in the scorecard band | Changing the rule after the result is forbidden either way; Paper costs nothing. The closed holdout trades lost, so this is weak evidence and the scorecard needs about a year | Interview | 2026-09-27 |
| D11 | Strategy identity | Reuse `trend_breakout`; rename title "GOLD Trend" → "Trend Breakout"; key and family unchanged | The holdout was spent on this key; a new key would be new code with an unscored holdout; family keys the Challenger rule | Interview | 2026-09-27 |
| D12 | Session | A separate Paper Session "BTC Trend", created by the operator; window disabled, Weekend Close off | Window and Weekend Close are per Session; GOLD needs Monday–Friday with Weekend Close | Interview | 2026-09-27 |
| D13 | Weekly review | Add BTCUSD H4 as an Arena of the `gold-review` skill; same name, allow-list and budget | Keeps the scheduled-task scripts unchanged; the app must price BTC like the research first | Interview | 2026-09-27 |
| D14 | Scope of the spread floor | BTCUSD only, through a Symbol Cost Profile; GOLD keeps its approved pricing. Whether GOLD should get the floor is an open question for the GOLD review | BTC's past spreads were 3–10× today's scaled figure, GOLD's about 2×; the floor would fail both GOLD strategies' every-period test, which deserves its own decision | Interview | 2026-09-27 |
| D15 | Trusted History | Per-symbol start (`research.trusted_from`), BTCUSD 2018-01-01, in the Evidence key | Evidence on the full feed would include fake 2013–15 intraday bars | Interview | 2026-09-27 |
| D16 | Sim weekends | The sim clock stays Monday–Friday; BTCUSD joins `DEFAULT_SYMBOLS`; weekend paths are unit-tested at explicit timestamps | Generating weekends would move the legacy golden inputs and E2E figures | Interview | 2026-09-27 |
| D17 | Seeded ledger | The 24 BTC trials and the spent H4 holdout are seeded into `research_trials` at startup (idempotent) | Without it, a later review could re-score the same holdout and would under-count trials | Interview | 2026-09-27 |
| D18 | Cost version | `COST_MODEL_VERSION` 1 → 2, and the swap-night mode goes into `EvidenceSpec.key` | Swap counting and floor rules changed; every Evidence key must change with them, and a later change of the terminal's triple day must not reuse old Evidence | Interview | 2026-09-27 |
| D19 | Learning on the BTC Arena | Auto-promotion stays off for BTCUSD H4 (the per-Arena default); Optimizer Runs and Challengers may still run and Shadow Trade | The validated set (entry 100) sits at the edge of the search space (entry 40–100), where shorter channels failed 2018–20; a promotion would swap in a set never scored on the holdout and restart the scorecard record (D10) | Interview | 2026-09-27 |

## Open Questions

- **GOLD and the spread floor (for the GOLD review, Monday).** With a 1× recorded-spread floor, both GOLD strategies fail "positive in every period" in 2010–17. GOLD Reopen is already slightly negative at the Evidence's 2× scaled spread. Decide whether GOLD Evidence should keep today's pricing.
- **Weekend swap on BTC.** The operator checks one real statement once a BTC position has been held over a weekend. Until then, D6 stands.

## Dependency Graph & Implementation Order

```
1 Costs: swap_every_night, rollover_nights(every_night), bar_floor, SymbolCostProfile, version 2
   └─> 2 Backtest/PaperTrader: bar spread into costs
         └─> 3 Trusted History: research.trusted_from; Evidence key + first_ts; research CLI
               └─> 4 Seeded ledger + spent holdout (needs the Candidate key and trusted_from)
2 ─> 5 Parity script on real data (read-only; gates 6–8)
1 ─> 6 SimBroker BTCUSD (DEFAULT_SYMBOLS) ─> 7 Engine tests at weekend timestamps; Cost Check swap
     8 Strategy title + UI label; editor 24/7 hint ─> 9 E2E
4, 5 ─> 10 gold-review skill: BTCUSD H4 Arena
all ─> 11 Verify: pytest, build, E2E, playwright-cli pass, MT5 smoke (read-only)
```

Order: 1 → 2 → 3 → 5 (stop if parity fails) → 4 → 6 → 7 → 8 → 9 → 10 → 11. Each step ends green.

## Implementation Checklist

1. Costs: the profile map, the `swap_every_night` flag, the every-night count, `bar_floor`, the version bump, tests.
2. Backtest: pass the bar spread through `PaperTrader.on_bar` and `backtest`; tests.
3. Trusted History: `trusted_from`, the Evidence key and first_ts, `evaluate_hypothesis`/`record_trial`, snapshot marking; tests.
4. Real-data parity: `crosscheck_app.py` reproduces ledger #6.
5. Seed: `seeds/btc-2026-09.json` and `seed_research`; holdout refusal; trial counts; tests.
6. SimBroker BTCUSD; confirm the golden and E2E inputs are unchanged.
7. Engine: weekend-timestamp tests; Cost Check swap for BTC.
8. Title "Trend Breakout" (backend and UI; update `test_research_cli.py`, which expects a label starting "GOLD Trend"); the editor's 24/7 hint.
9. E2E spec for the BTC Assignment.
10. The gold-review skill: the BTCUSD H4 Arena, the no-cost-edits rule and the BTC auto-promotion rule; the allow-list test still passes; the profile and seed pin tests.
11. Verify: `uv run pytest`, `npm run build`, `cd e2e && npm test`, a playwright-cli pass, `uv run pytest -m mt5 -o addopts=""`.
