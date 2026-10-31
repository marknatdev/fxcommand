# Spec: Layer B (Forward Proof) and Layer C (Autonomy) — Order-Free System

- **Status:** accepted Wave 0 contract, 2026-10-30.
- **Inputs:** Owner decisions from 2026-10-30 grill (`layer-bc-grill-decisions`), [intent](../intent/vibe-desk.md), [Layer A spec](layer-a-verdict-engine.md), [glossary](../../CONTEXT.md), [ADRs](../adr/).
- **Hard Rule:** **No orders, ever.** Nothing in `src/` may call MT5 order or position functions. MT5 is reached only through `real_api()`, which wraps it in `ReadOnlyMt5`. Order-placing cards are parked and out of scope.

---

## 1. Scope and Boundary

### In Scope (Order-Free Core)
1. **Forward Arena (CCC-66):** Paper simulation runtime tracking CANDIDATE strategies on live closed MT5 bars.
2. **Two Fill Tracks:** Concurrent backtest-cost track (Spread Floor) and live-spread track (tick spread at signal).
3. **Leaderboard & Gates (CCC-67, CCC-69):** Ranking by forward net R, Demo-Ready badge with owner override, and computed live gate.
4. **Fill-Bar Rule (CCC-71 backtest & Arena):** Signal at $t$, time filter re-checked at fill bar $t+1$; if failed, entry skipped.
5. **Uptime & Gap Handling (CCC-73):** Live MT5 bar poll, cache append, catch-up replay on restart, health log.
6. **Fill Comparison Report (CCC-72 order-free):** Statistical divergence between backtest-cost track and live-spread track.
7. **Night Shift (CCC-75):** Overnight autonomous Trial research loop bounded by Trial and token caps, reporting via 07:00 Bangkok brief.
8. **Regime Desk (CCC-76):** Daily feature extraction, typed LLM on/off toggle, always-on vs regime-gated virtual twins.
9. **Custom Blocks (CCC-77):** AI-authored blocks in `blocks/custom/` gated by property tests and explicit owner UI approval.
10. **CCC-79 & CCC-80 Decisions:** Open Holdout trade closed at last bar with full cost; remove unused stop distance in `atr_target`.

### Out of Scope (Order-Placing Cards)
The following cards involve placing, managing, or closing orders on MT5 accounts and are **strictly excluded** from code:
- **CCC-68:** Demo gate execution (placing orders on XM demo).
- **CCC-70:** Owner live switch and kill switch.
- **CCC-82:** Live EA execution.
- **CCC-72 (demo half):** Comparison with live demo terminal fills.
- **CCC-71 (live half):** Live broker order transmission.

A separate worker writes the design specification for order placement in [docs/design/order-side.md](../design/order-side.md). Code in `src/` must never touch order or position endpoints.

---

## 2. Forward Arena (CCC-66)

The Forward Arena is an order-free paper trading environment running inside the Vibe Desk backend process.

### 2.1 Live Closed Bar Feed
- **Polling Loop:** Runs on a timer inside `vibedesk.app.scheduler` (every 10 seconds).
- **Read-Only Ingestion:** Calls MT5 through `ReadOnlyMt5` (`copy_rates_range`).
- **Closed Bars Only:** A bar is accepted only after its timeframe interval has elapsed:
  $$\text{bar\_open\_time} + \text{timeframe\_seconds} \le \text{current\_time}$$
  Bars currently forming are never evaluated.
- **Cache Persistence:** Newly closed bars are converted to UTC and appended to the Parquet cache (`data/bars/{symbol}/{timeframe}/`).

### 2.2 Enrolment and Lifecycle
- **Eligibility:** Only Hypotheses that achieved a `CANDIDATE` or `PASS` Verdict in the Gauntlet may enter.
- **Owner Action Only:** The AI cannot promote a strategy. Enrolment requires the owner to click "Send to Arena" (`POST /api/arena/strategies`).
- **Initial Capital:** Each enrolled strategy receives a dedicated virtual **Paper Account** with an initial balance of **$10,000 USD**.
- **Sizing:** Position size is derived deterministically from the Spec's `fixed_risk` sizing block using account equity at the signal bar.

### 2.3 Two Parallel Fill Tracks
Every enrolled strategy evaluates two fill tracks simultaneously on the same signals:
1. **Backtest-Cost Track (Primary Benchmark):**
   - Price: Bar open at $t+1$.
   - Cost: $\max(\text{bar spread}, \text{Spread Floor}) + 1\text{ point slippage}$.
   - Swap: Triple swap on Wednesday, zero credit for positive swap.
   - Purpose: Direct parity comparison with the Gauntlet backtest.
2. **Live-Spread Track (Execution Reality):**
   - Price: Bar open at $t+1$.
   - Cost: Live MT5 spread measured at signal close + 1 point slippage.
   - Swap: Identical swap rules.
   - Purpose: Measures real-market slippage and spread deterioration without risk.

### 2.4 Gap Catch-Up on Restart
When the backend process restarts or recovers from network outage:
1. Identify the timestamp of the last processed closed bar for each strategy.
2. Query the Parquet cache for all subsequent closed bars up to current time.
3. Replay missing bars chronologically through `ArenaStrategy.catch_up(bars)`.
4. Log a gap catch-up event in the Ledger (`arena_actions`).

### 2.5 Owner Controls
Owner-only endpoints protected by the owner cookie:
- `POST /api/arena/strategies/{id}/pause`: Freeze strategy; open positions remain open or settle at stop/target, but no new entries trigger.
- `POST /api/arena/strategies/{id}/resume`: Resume evaluating signals.
- `DELETE /api/arena/strategies/{id}`: Terminate strategy and mark status as `removed`.
- `POST /api/arena/stop`: Emergency global pause for all Arena strategies.
Every control action is permanently logged to the Ledger table `arena_actions`.

---

## 3. Leaderboard and Gates (CCC-67, CCC-69)

### 3.1 Leaderboard Ranking
- **Sort Metric:** Forward Net R (sum of closed trade R).
- **Trade Threshold:** Minimum **10 forward closed trades** before a strategy is ranked on the board.
- **Displayed Metrics:**
  - Forward Net R
  - Forward Profit Factor (PF)
  - Maximum Equity Drawdown (%)
  - Trade Count
  - Win Rate (for information only; not used in gates)
  - **Decay Flag:** Boolean indicating if forward PF has dropped below the 95% bootstrap lower bound of the Gauntlet training folds.

### 3.2 Demo-Ready Badge
A strategy automatically earns the **Demo-Ready Badge** when all of the following conditions are met:
1. **Duration:** $\ge 4$ weeks in the Arena.
2. **Sample Size:** $\ge 30$ forward closed trades.
3. **Profit Factor:** Forward $\text{PF} \ge 1.10$.
4. **Drawdown:** Maximum Peak-to-Trough Drawdown $< 6.0\%$.
5. **Decay:** No active decay flag.

When earned:
- A badge icon is rendered next to the strategy in the UI.
- A Telegram notice of kind `candidate` is emitted (`[DEMO READY] Strategy #X qualified`).

### 3.3 Owner Override ("Mark Ready Anyway")
The owner may bypass the Demo-Ready criteria at any time via `POST /api/arena/strategies/{id}/override`.
- Requires a non-empty `reason` string.
- Appends an audit row to `arena_overrides`.
- Strategy displays the badge with an `(Owner Override)` indicator.

### 3.4 Live Gate (Computed & Shown Only)
The Live Gate evaluates whether forward performance satisfies statistical standards for live capital:
1. **Trade Count per Timeframe:**
   - M15: $\ge 100$ trades
   - H1: $\ge 60$ trades
   - H4: $\ge 40$ trades
   - D1: $\ge 30$ trades
2. **Statistical Confidence:** 1-sided 95% bootstrap lower bound of Profit Factor $> 1.00$.
3. **Drawdown:** Maximum Drawdown $< 6.0\%$.
Status is computed and presented on the strategy card as `Live Gate: Qualified` or `Live Gate: Pending (X/N trades, PF LB: Y)`. No orders are placed.

---

## 4. Fill-Bar Rule (CCC-71)

### Rule Definition
A trading signal is generated on bar $t$ based on completed indicators. The earliest possible fill occurs at bar $t+1$ open.
- If the Spec contains a time filter (e.g. `session_window` or `day_of_week`), the filter must evaluate true on **both** bar $t$ (signal generation) and bar $t+1$ (execution open).
- If bar $t+1$ violates the time filter (e.g., weekend close, session end), the fill is **skipped**.
- The fill skip event is logged with the timestamp and reason.

### Dual Engine Application
This rule applies identically to:
1. The **Forward Arena** runtime.
2. The **Gauntlet backtest engine** (`vibedesk.engine.simulate`).
Because this rule changes historical trade sequences, its implementation bumps `RULES_VERSION` from 3 to 4 (see [ADR 0008](../adr/0008-fill-bar-time-filter-rule.md)).

---

## 5. Uptime, Health and Fill Comparison (CCC-72, CCC-73)

### 5.1 Host Uptime & Health Log
- A background health monitor logs a heartbeat every 60 seconds to `data/health.jsonl`.
- Records: timestamp, MT5 connection status, last received tick time per symbol, memory usage.
- If no bar arrives within $2 \times \text{timeframe}$ of expected close during market hours, or if MT5 drops:
  - System logs an alert.
  - Sends Telegram notice: `[HEALTH WARNING] Feed delayed for SYMBOL on TF`.

### 5.2 Fill Comparison Report
Accessible via `GET /api/arena/fills/report`:
- Aggregates all closed forward trades across both fill tracks.
- Columns: Strategy ID, Symbol, Entry Time, Exit Time, Backtest-Track R, Live-Spread Track R, Divergence ($\Delta R$), Spread Delta.
- Flags systematic spread widening or execution slippage.

---

## 6. Night Shift (CCC-75)

Night Shift is an autonomous overnight research loop designed to test queued ideas and parameter mutations while the owner sleeps.

### 6.1 Constraints and Limits
- **Trial Cap:** Maximum **20 Trials** per night (configurable in Settings).
- **Token Budget Cap:** Maximum **50% of the daily token cap** (configurable in Settings).
- **Immutable Sandbox:**
  - May run Gauntlet Trials only.
  - Each Trial is charged to the Idea Family's Trial Budget in the Ledger.
  - **Never** spends Holdout data.
  - **Never** sends strategies to the Arena.

### 6.2 Schedule and Reporting
- **Execution Window:** Starts at 01:00 Asia/Bangkok time.
- **Morning Brief:** Dispatched via Telegram at **07:00 Asia/Bangkok time**:
  - Total Trials run overnight.
  - Number of CANDIDATE verdicts discovered.
  - Tokens consumed.
  - Stop reason (`completed`, `hit_trial_cap_20`, `hit_token_budget_50%`, `error`).

---

## 7. Regime Desk (CCC-76)

Regime Desk adds automated macro-regime gating to Arena strategies as an experimental A/B test.

### 7.1 Daily Feature Computation
Every day at 00:05 Bangkok time, code computes regime metrics:
- Volatility: 14-period ATR relative to 100-period ATR median (high / normal / low).
- Trend: ADX(14) level and 50-period MA slope.

### 7.2 Typed LLM Decision
A single daily structured LLM call receives the computed features and the active strategy list.
- Response format: JSON mapping strategy ID to `{ "active": bool, "reason": RegimeReason }`.
- Allowed reasons: `HIGH_VOLATILITY`, `LOW_VOLATILITY`, `STRONG_TREND`, `CHOPPY`, `RANGE_BOUND`, `NORMAL`.

### 7.3 Virtual Strategy Twins
Each strategy enrolled in the Arena with regime gating enabled runs as two independent accounts:
1. **Always-On Twin (`twin_type="standard"`):** Executes all signals continuously.
2. **Regime-Gated Twin (`twin_type="regime_gated"`):** Executes signals only when Regime Desk decision is `active=true`.
Both twins run in parallel to measure whether regime filtering improves forward edge.

---

## 8. Custom Blocks (CCC-77)

Allows expanding the Block Library with AI-authored components under strict isolation.

### 8.1 Isolation Contract
- Custom blocks are written exclusively to `src/vibedesk/blocks/custom/{name}.py`.
- The AI is prohibited from modifying core block files in `src/vibedesk/blocks/`.
- Every custom block must implement the `BlockDef` protocol and declare typed parameter bounds.

### 8.2 Safety Verification
Before a custom block can be approved:
1. **Syntax & Import Validation:** Must parse cleanly and import without side effects.
2. **Look-Ahead Test:** Must pass the property test `test_custom_block_lookahead` (future bars cannot alter past signals).
3. **Unit Tests:** Must include an accompanying unit test file.

### 8.3 Owner Approval Gate
- An unapproved block has `approved=False` in the registry and is rejected if included in any Spec.
- The owner must inspect the code in the UI and click "Approve" (`POST /api/blocks/custom/{name}/approve`).
- The approval event and code SHA-256 are stored in the Ledger table `custom_blocks`.

---

## 9. Architectural Fixes (CCC-79 & CCC-80)

### 9.1 CCC-79: Holdout Open Trade Settlement
- In walk-forward Holdout runs, any trade remaining open at the final bar of the dataset is forcibly closed on the close of that final bar.
- Charged full exit transaction costs: Spread Floor + 1 point slippage.
- Documented in [ADR 0013](../adr/0013-holdout-open-trade-settlement.md).

### 9.2 CCC-80: Unused Parameter Cleanup in `atr_target`
- In `src/vibedesk/blocks/exits.py`, `atr_target` received an unused `stop_distance` parameter.
- The parameter is removed from the block definition and call signature.
- Historical trade outputs are identical; documented in [ADR 0014](../adr/0014-remove-unused-atr-target-stop-distance.md).

---

## 10. Summary Matrix

| Milestone | Component | Order-Free Implementation | Owning Team |
|---|---|---|---|
| CCC-66 | Forward Arena Core | `vibedesk.arena`: Strategy, Account, Tracks, Scheduler runtime | Arena Core (Wave 1) |
| CCC-71 | Fill-Bar Rule | `vibedesk.engine.simulate` & Arena step: skip if $t+1$ invalid | Engine (Wave 1) |
| CCC-75 | Night Shift | `vibedesk.nightshift`: 20-Trial cap, 50% token cap, 07:00 brief | Night Shift (Wave 1) |
| CCC-77 | Custom Blocks | `vibedesk.blocks.custom`: isolation, look-ahead test, approval gate | Custom Blocks (Wave 1) |
| CCC-79 | Holdout Open Trade | Engine Holdout settlement at last bar close with full costs | Engine (Wave 1) |
| CCC-80 | `atr_target` Cleanup | Remove unused `stop_distance` parameter | Engine (Wave 1) |
| CCC-67 | Leaderboard & Demo Badge | Rank by net R; 4 wk / 30 trd / PF 1.1 / DD < 6% + override | Leaderboard (Wave 2) |
| CCC-69 | Live Gate | Per-TF trade count, bootstrap PF LB > 1.0 (shown only) | Leaderboard (Wave 2) |
| CCC-72 | Fill Report | Compare backtest track vs live-spread track divergence | Uptime & Report (Wave 2) |
| CCC-73 | Uptime & Gaps | Health logger, gap catch-up replay, Telegram disconnect alert | Uptime & Report (Wave 2) |
| CCC-76 | Regime Desk | Daily features, typed LLM on/off, always-on vs gated twins | Regime Desk (Wave 2) |
| CCC-52+ | Arena UI | UI views for Arena, Leaderboard, Night Shift, Custom Blocks | UI Team (Wave 2) |
