# Spec: Layer A, the Verdict Engine

- **Status:** accepted, 2026-10-21.
- **Inputs:** [intent](../intent/vibe-desk.md), [one-pager](../ideas/vibe-desk.md), [glossary](../../CONTEXT.md), [ADRs](../adr/).
- **Vocabulary:** this spec uses the glossary's terms exactly.

## 1. Objective

The owner types an Idea into the Desk chat. A Verdict Card the owner can trust follows within minutes. The Research Desk turns the Idea into one or more Hypotheses, each built from Blocks. It runs Trials through the Gauntlet and records every Trial in the Ledger. It explains the outcome without inventing a single number.

Layer A never trades. It has no live-feed runtime, no virtual accounts and no orders. The Forward Arena is Layer B.

**Success:**
- An Idea gets to a Verdict in ≤ 10 minutes.
- One Trial takes ≤ 60 s for GOLD M15 over the full Research Window.
- The Ledger's Trial counts can be reproduced from stored records.
- Not one Verdict Card figure differs from stored Trial data.

## 2. Environment facts (probed read-only, 2026-10-21)

| Fact | Value |
|---|---|
| Account | XMGlobal-MT5 7, demo, USD, hedging. The terminal is running. |
| Python | The MetaTrader5 wheel 5.0.6231 works on **Python 3.12** (checked with uv). The system Python is 3.10. |
| Server clock | Epoch values returned by MT5 are **server-local time**. Today they run UTC+3.00. XM's server time is New York time + 7 h, so DST follows the US. |
| Bar boundaries | D1 opens at server 00:00, which is 17:00 New York. H4 opens at 00, 04 … 20 server time. |
| GOLD history | Real M1/M15 data starts **mid-2014**. Earlier years hold one bar per day, which is unusable. |
| FX majors history | Real M15 data from 1999, M1 from 2010 or earlier. |
| Bar spread | Each bar stores the minimum spread inside it. GOLD bars show 30–40 points; ticks over the last 14 days show 55 (p50), 57 (p90). EURUSD bars show 16; ticks show 20. |
| Swap | `swap_mode = 1` (points per lot per night). Triple swap is charged on Wednesday (`swap_rollover3days = 3`). |
| Ticks | ≥ 24 months available. Not used in Layer A. |
| OmniRoute | Installed on the laptop (`~/.omniroute`) but not running at probe time. |

## 3. Architecture

### 3.1 One backend process

The single backend process is FastAPI on `127.0.0.1` only (ADR 0006).

- It is the only writer of the SQLite store (`data/desk.db`) and of the Parquet bar cache (`data/bars/`).
- The MCP server is a separate thin process that calls the HTTP API.
- The React UI is built into static files and served by the backend.

### 3.2 Deep modules

Each module sits behind a small interface.

| Module | Interface (essence) | Owns |
|---|---|---|
| `market` | `bars(symbol, tf, start, end, holdout: HoldoutGrant \| None) -> Bars`; `spread_floor(symbol) -> Floor`; `refresh()` | Import, UTC conversion, the Parquet cache, data-quality masks, the **Holdout guard** |
| `blocks` | `library() -> [BlockDef]`; `validate(spec_json) -> StrategySpec`; `compile(spec, params, bars) -> Signals` | The Block Library, the spec schema, parameter grids |
| `engine` | `simulate(signals, bars, m1, cost: CostSnapshot) -> Trades` | Fills, stops and targets, same-bar conflicts, costs and swap |
| `gauntlet` | `run(hypothesis, ctx) -> GauntletResult`; `holdout(hypothesis, grant) -> HoldoutResult` | Walk-forward, the Coin-Flip Baseline, Deflated Sharpe, PBO, the Prosecutor, the Pass Bar |
| `ledger` | `register(hypothesis)`, `record(trial)`, `counts(family)`, `budget(family)`, `request_holdout`, `approve_holdout` | Append-only records, Trial counting, budgets, Holdout Requests |
| `desk` | `chat(family_id, message) -> stream[Event]` | The Research Desk loop, its tools, the Grounding Rule, the token budget |
| `notify` | `send(event)` | Telegram notices and the daily digest |

### 3.3 Seams

There is one seam per external dependency, and each has a real adapter and a fake.

| Seam | Real adapter | Fake adapter (tests, E2E) |
|---|---|---|
| `BarSource` | `Mt5BarSource`. Read only: it uses only `initialize`, `copy_rates_range`, `copy_ticks_range`, `symbol_info`, `symbol_info_tick` (the §10 clock check), `symbol_select`, `last_error` and `shutdown`. An allowlist guard test enforces this (M3 plan). | `SyntheticBarSource`: seeded random walk, plus planted edge series with known answers |
| `LLM` | `OmniRouteLLM`: OpenAI-compatible chat completions with tools, at `http://127.0.0.1:20128/v1` (configurable), combo `vibe-desk` | `ScriptedLLM`, which replays recorded tool-call scripts |
| `Notifier` | `TelegramNotifier`, which calls the Bot API over httpx | `RecordingNotifier` |
| `Clock` | `SystemClock` | `FixedClock` |

### 3.4 Stack

| Part | Choice |
|---|---|
| Backend | Python 3.12, uv, FastAPI, pydantic v2, numpy, pandas, pyarrow, numba (for the path-dependent fill loop), stdlib sqlite3, httpx, the official `mcp` Python SDK |
| Frontend | React, Vite, TypeScript, lightweight-charts (price and trades), ECharts (equity, folds, baseline histogram) |
| Tests | pytest, hypothesis (property tests on the engine), @playwright/test for E2E |

## 4. Market data

- **Universe:** GOLD, EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF and NZDUSD. Timeframes M15, H1, H4 and D1, plus M1, which is stored only for resolving same-bar conflicts.
- **Time:**
  - MT5 epoch values are treated as naive server time.
  - `utc = to_utc(server_naive − 7 h, tz="America/New_York")`.
  - Everything is stored in UTC. Bars keep their server-clock boundaries, so a D1 bar runs from 17:00 to 17:00 New York time.
  - The UI shows Asia/Bangkok time.
  - Session Blocks are defined in exchange-local time (London `Europe/London`, New York `America/New_York`).
- **Data-quality mask:** bars before a symbol's first real intraday month are excluded, along with days with fewer than 50% of their expected bars. For GOLD the first usable month is detected automatically (expected mid-2014). The first walk-forward training window therefore starts at the later of 2014-01-01 and the first usable month.
- **Holdout guard (ADR 0002):** `market.bars()` raises an error for any range reaching 2025-01-01 or later unless it is given a valid `HoldoutGrant`. Only `ledger.approve_holdout` can create one. The Research Desk's tools, the engine, the baseline and the gauntlet all get bars only through `market.bars()`. A test asserts that no other code path reads the cache.
- **Refresh:** nightly at 02:00 Bangkok time, read-only and incremental. Spread Floors are re-measured weekly. Each floor is the p50 of the last 14 days of ticks, in points.
- **Floor units:** today's floor is applied to all history. GOLD's price was about 3× lower in 2014, so early folds are charged more than they really cost. This is a deliberate, conservative choice; it will be noted in ADR 0004.

## 5. Strategies

### 5.1 Strategy Spec (ADR 0001)

```json
{
  "name": "london-breakout-trend",
  "symbol": "GOLD",
  "timeframe": "M15",
  "direction": "both",
  "entry":  {"block": "range_breakout", "params": {"session": "london_open", "range_bars": [4, 8]}},
  "filters": [{"block": "ma_slope", "params": {"period": [50, 100, 200], "min_slope_atr": 0.0}}],
  "exit":   {"stop": {"block": "atr_stop", "params": {"mult": [1.5, 2.0, 2.5]}},
             "target": {"block": "r_target", "params": {"r": [2.0, 3.0]}},
             "time": {"block": "time_exit", "params": {"bars": 32}}},
  "sizing": {"block": "fixed_risk", "params": {"risk_pct": 1.0}}
}
```

- A list in `params` is a grid dimension. A scalar is fixed.
- The full grid may have at most **50 combinations**. Validation rejects anything larger.
- Every Spec needs a stop. There is at most one open position per Hypothesis.
- `direction` is `long`, `short` or `both`.

### 5.2 Block Library v1 (the starter set the owner agreed to)

| Kind | Block | Params |
|---|---|---|
| Entry | `ma_cross` | fast, slow, type (sma/ema) |
| Entry | `donchian_breakout` | period |
| Entry | `rsi_threshold` | period, level, mode (cross_up/cross_down) |
| Entry | `bollinger_touch` | period, stdev, mode (fade/break) |
| Entry | `range_breakout` | session (asia, london_open, ny_open), range_bars |
| Entry | `pullback_to_ma` | trend_period, pullback_period, depth_atr |
| Filter | `ma_slope` | period, min_slope_atr |
| Filter | `adx` | period, min, max |
| Filter | `atr_regime` | period, lookback, percentile_min, percentile_max |
| Filter | `session_window` | zone, start, end (exchange-local); judges the entry bar |
| Filter | `day_of_week` | days (server clock); judges the entry bar |
| Exit | `atr_stop` | period, mult |
| Exit | `atr_target` | period, mult |
| Exit | `r_target` | r |
| Exit | `atr_trailing` | period, mult |
| Exit | `time_exit` | bars |
| Exit | `opposite_signal` | — (closes, and reverses when the Spec's entry allows it; see §6.1) |
| Sizing | `fixed_risk` | risk_pct |

Every Block has a pure, vectorised implementation that uses only bars at or before the signal bar. A property test checks that no Block looks ahead: the signal at bar *t* must be unchanged when the bars after *t* are altered.

**One exception, by design (2026-10-22):** time filters (`session_window`, `day_of_week`) judge when the trade would open, the entry bar, so they read the next bar's open time. That is the clock, never a price. For example, "London 08:00–16:00" means the trade opens inside that window, and a D1 Friday signal enters on Monday. On the last bar, which has no next bar yet, they use one bar later on the clock.

## 6. Engine and Gauntlet

### 6.1 Fills and costs (ADR 0004)

- **Signals and entries:** a signal is evaluated on the bar's close. The position enters at the next bar's open.
- **Prices:** bars are bid prices. A long entry pays `open + spread`. Short positions exit at ask.
- **Spread:** `max(bar spread, Spread Floor)`.
- **Slippage:** 1 point per side, on every fill.
- **Stops and targets:** checked inside each bar against high and low, with ask-side prices for shorts.
  - If the stop and the target are both touched in one bar, the M1 bars settle the order.
  - If it is still ambiguous, or M1 data is missing, the stop wins.
- **Exits** (the engine docstring in `engine/simulate.py` holds the full fill rules):
  - **Priority:** when several exits apply at once, the first of stop, target, trailing stop, time exit, opposite signal wins.
  - **A stop already breached at the open** exits at that open, including a stop inside the spread on the entry bar.
  - **Trailing stop:** moves only at bar closes and only in the trade's favour.
  - **Time exit:** leaves at the open `bars` bars after the entry bar.
  - **Opposite signal:** listens to the entry Block alone (no filters, no direction) and leaves at the next open.
  - **Stop and reverse** (2026-10-22): when the Spec's own entry for the other side (filters and direction included, with a usable stop) fires on the same bar as the opposite signal, the position closes at the next open and the reverse opens at that same open. A time exit due at that open is recorded as the closing reason, and the reversal still happens. Without a valid entry on the other side, the opposite signal only closes.
- **Swap:** charged in points per lot for each night held at the server rollover, ×3 on Wednesday, using the swap values in the Trial's `CostSnapshot`.
- **Reproducibility:** every Trial stores its `CostSnapshot` (floor, swap long and short, slippage, point size and its source date), so the Ledger stays reproducible.
- **Units:** results are in R (glossary). The equity curve and drawdown are also shown at 1% risk on $1,000. Only the R metrics feed the Pass Bar.

### 6.2 Walk-forward

- **Folds:** rolling 3 years of training and 1 blind year, giving 8 Walk-Forward Folds (2017 … 2024).
- **Parameter choice:** in each fold, choose the grid combination with the best training profit factor among those with ≥ 30 training trades. Trade it blind for the fold year.
- **Stitching:** the blind years are stitched into the Trial's out-of-sample record.
- **Persistence:** every combination's per-fold results are stored, because PBO and Deflated Sharpe need them.

### 6.3 Gauntlet checks and the Pass Bar (ADR 0007)

A Trial's Verdict is **CANDIDATE** only if every check passes. Otherwise it is **REJECT**, and the Closest Miss is the failed check whose ratio of value to threshold is nearest 1.

| Check | Pass when |
|---|---|
| Out-of-sample profit factor | ≥ 1.2 over the stitched blind years, with ≥ 100 trades |
| Fold consistency | ≥ 5 of 8 folds have positive R |
| Spread stress | profit factor ≥ 1.05 when the floor and bar spreads are doubled |
| Coin-Flip Baseline | net R is above the 95th percentile of 1,000 runs. Each run uses the same trade count per fold, the same session-hour distribution, the same long/short mix, the same exits and the same Cost Model. Seeded by what the Trial judges (its Combinations, bars, M1 bars and the rules version), so re-running an unchanged Spec on unchanged data draws the same runs (rules version 2). |
| Deflated Sharpe | probability ≥ 0.95 (Bailey and López de Prado). The return series is the **daily R** over the stitched blind years, with every trading day counted including zero days. T, skew and kurtosis come from that series. N and the variance of Sharpe across trials follow §6.4. |
| PBO | ≤ 0.3 by CSCV over 16 time blocks of the blind years and the Spec's grid combinations. A grid of one combination gives "n/a", which neither passes nor fails. |
| Prosecutor | no red flag. A red flag is any of these: the top 5 trades make > 50% of net R; the median profit factor of neighbouring grid combinations (±1 step) is < 1.0; for `both`, any side with ≥ 30 trades has profit factor < 1.0; any ATR-tercile regime with ≥ 30 trades has profit factor < 0.9. The LLM writes the narrative only from these results. |

The **Holdout** check, run only once an approved Holdout Request exists:
- It uses the parameters chosen on the last 3 years (2022–2024).
- It runs from 2025-01-01 to the end of the data.
- The Hypothesis gets **PASS** if profit factor ≥ 1.1 with ≥ 30 trades. Otherwise it gets **HOLDOUT FAIL**, which is final for the Idea Family.

**Holdout Request gate:**

| Situation | Outcome |
|---|---|
| Verdict isn't CANDIDATE | Refused |
| The Trial ran under other trade rules | Refused with `RULES_CHANGED` (`src/vibedesk/rules.py`) |
| At the family's N today, the Trial's Deflated Sharpe is below 0.95 | Refused with `DEFLATED_SHARPE_FELL`. Trials of sibling Hypotheses raise N too (§6.4). |
| Expected Holdout trades < 30 | Refused with `INSUFFICIENT_TRADES`. Nothing is spent. |
| Otherwise | Recorded as awaiting the owner |

The Holdout run checks the first three rows again, before it fetches a single Holdout bar.

Expected Holdout trades = blind-year trades per year × Holdout years.

### 6.4 What a Trial counts and which N deflates (decided 2026-10-21, ADR 0003)

- **Trial = one Strategy Spec run through the walk-forward.** It adds +1 to the Ledger and +1 to the Idea Family's Trial Budget. The budget is 25, and the owner can extend it by +25 at a time.
- **Deflated Sharpe N = all grid combinations evaluated across the Idea Family's Trials.** The variance of Sharpe comes from those combinations' blind-year Sharpe ratios.
- **The desk-wide Trial count** is shown on the Ledger page for information only. It does not deflate, because otherwise the bar would rise forever and nothing could ever pass.
- **Anti-gaming:** an Idea Family is fixed when its first Hypothesis is registered. The Research Desk must attach a new Idea to an existing family when its Spec shares the same entry Block and symbol, so the count can't be reset by renaming.
## 7. Research Desk

### 7.1 Loop

- A chat message goes to the LLM with the tool definitions.
- Tool calls run, and their results go back to the LLM.
- At most 12 tool calls per message.
- The Desk streams events to the UI over SSE: `message`, `tool_started`, `trial_progress`, `verdict`.

### 7.2 Tools

| Tool | Effect |
|---|---|
| `list_blocks` | Returns the Block Library |
| `describe_data(symbol, tf)` | Returns statistics on the Research Window only: coverage, ATR levels, Spread Floor |
| `draft_spec(json)` | Validates a Spec and returns errors or a grid size. No Trial is counted. |
| `register_hypothesis(family, spec)` | Freezes the Hypothesis in the Ledger |
| `run_trial(hypothesis_id)` | Runs the Gauntlet. Counts as a Trial. Refused once the Trial Budget is spent. |
| `get_trial(id)` | Returns stored results |
| `request_holdout(hypothesis_id)` | Goes through the gate in §6.3 |

The Research Desk cannot approve a Holdout, extend a budget or change the Pass Bar.

### 7.3 Grounding Rule (ADR 0005)

**Validator.** Assistant prose may not contain any digit, except inside these tokens:
- symbol names from the universe;
- timeframe names (M1, M15, H1, H4, D1);
- Block names;
- placeholders: `{{trial:ID.metric}}`, `{{spec:ID.param}}`, `{{hyp:ID.field}}`, `{{date:ISO}}`.

The app renders each placeholder from stored data. An unknown placeholder fails validation.

**When validation fails:**
- The reply is regenerated with a correction message, up to 2 retries.
- After that the Desk shows the Verdict Card alone, with a note that the explanation was withheld.

**Verdict Cards** are rendered only from `GauntletResult` data, never from LLM text.

### 7.4 Token budget

- A daily token cap is set in Settings and counted from OmniRoute's `usage` fields.
- At 80% of the cap, Telegram sends a warning.
- At 100%, the Desk pauses. Manual Trials, using a Spec typed or pasted into the UI, still work.

## 8. Interfaces

### 8.1 HTTP API

- **Address:** `127.0.0.1` only, with no login (one owner on one laptop).
- **Owner-only actions:** approve a Holdout, extend a budget, change Settings. These need the `owner` cookie, which is `SameSite=Strict` and set by the UI page. The MCP server never has it.

| Area | Endpoints |
|---|---|
| Families and Hypotheses | `GET/POST /api/families`, `GET /api/families/{id}`, `GET /api/hypotheses/{id}` |
| Chat | `POST /api/families/{id}/chat` (SSE) |
| Trials | `POST /api/hypotheses/{id}/trials`, `GET /api/trials/{id}` |
| Blocks | `GET /api/blocks` |
| Ledger | `GET /api/ledger` |
| Holdout | `POST /api/hypotheses/{id}/holdout-requests`, `POST /api/holdout-requests/{id}/approve` (owner) |
| Budget | `POST /api/families/{id}/budget` (owner) |
| Settings | `GET/PUT /api/settings` (PUT is owner-only) |
| Health | `GET /api/health` (MT5, OmniRoute, last refresh) |

### 8.2 MCP server (stdio, calls the API)

| Kind | Tools |
|---|---|
| Read | `list_ideas`, `get_verdict`, `get_ledger_stats`, `list_blocks`, `get_trial` |
| Act (counted in the Ledger) | `submit_idea`, `run_trial`, `request_holdout` |

The MCP server has no approve, budget or settings tools.

### 8.3 Web pages

| Page | Contents |
|---|---|
| Desk | Chat with live Trial progress and inline Verdict Cards. A Verdict Card shows the stitched equity curve, the per-fold bars, a baseline histogram with the strategy marked, the Pass Bar checks with the Closest Miss, win rate (shown only, never judged), and the Cost Snapshot. |
| Ideas | Every Idea Family, Hypothesis and Verdict, with filters |
| Ledger | Trial counts (per family and desk-wide), the current deflation, budgets, and Holdout Requests with approve buttons |
| Settings | OmniRoute URL, combo and token cap; Spread Floors (read-only, with the date measured); Telegram; data-refresh status |

The layout works at phone width (375 px) with no horizontal scroll.

### 8.4 Telegram

- Sends the CANDIDATE, PASS and HOLDOUT FAIL Verdicts, Holdout Requests awaiting the owner, and the token-budget warning.
- Sends a daily digest at 21:00 Bangkok time with the REJECT count, Trials run and tokens used.
- It is push-only.
- The bot token is set with `scripts/setup-telegram.ps1` and stored in `data/secrets.json`, which is git-ignored. It never appears in logs.

## 9. Safety and guard tests

These run in CI on every change:
1. **No orders:** a test fails if `order_send`, `order_check`, `positions_` or `orders_` appear anywhere in `src/`.
2. **Holdout:** every public data path refuses dates ≥ 2025-01-01 without a grant. A forged or expired grant is refused.
3. **Ledger:** `run_trial` always adds exactly 1 to the Ledger. The Ledger has no update or delete statements outside migrations.
4. **Grounding:** prose that contains digits is rejected; whitelisted tokens and placeholders pass.
5. **Look-ahead:** the property test from §5.2 runs on every Block.
6. **Known answers:** `SyntheticBarSource` provides (a) a random walk, on which a strategy must not get CANDIDATE more often than a set rate across seeds, and (b) a planted edge, which must get CANDIDATE.
7. **Live smoke:** the only test that touches MT5 is a read-only smoke test, `desk smoke`, run by hand.

## 10. Milestones

Each milestone ends with its tests green.

| # | Milestone | Done when |
|---|---|---|
| M1 | Pure core: `blocks` and `engine` on `SyntheticBarSource` | Hand-calculated golden trades match. Look-ahead property tests pass. A 12-year M15 simulation of one combination takes ≤ 0.5 s. |
| M2 | `gauntlet` and `ledger` | Known-answer tests (§9.6) pass. A Trial on synthetic 12-year M15 data takes ≤ 60 s. Holdout and Ledger guard tests pass. |
| M3 | `market` with `Mt5BarSource` | A read-only import fills the cache. The UTC conversion is checked against `symbol_info_tick` times. Spread Floors are measured. `desk smoke` passes. |
| M4 | `desk` with `ScriptedLLM` and then `OmniRouteLLM` | A scripted Idea reaches a Verdict end to end. Grounding tests pass. The Trial Budget and token cap are enforced. |
| M5 | API and web UI (4 pages) | Playwright E2E passes on the fakes. The UI works at 375 px. |
| M6 | MCP server and Telegram | MCP tools work against the API. The owner-only refusals are tested. Notices arrive in `RecordingNotifier`. |
| M7 | Whole-app test | E2E on deterministic fakes. An exploratory click-through with playwright-cli covers every page, the console and phone width. `CLAUDE.md` holds the run and test commands. |

## 11. Not in Layer A

- The Forward Arena, virtual accounts, any live-feed runtime, the demo or live gate, and a kill switch (Layer B).
- The Night Shift, the Regime Desk and AI-written custom Blocks (Layer C).
- Order code of any kind.
- Tick-level backtests.
- Portfolio or multi-symbol sizing.
- Telegram chat.
- Any login.
