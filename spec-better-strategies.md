# Spec: Better Strategies, GOLD First

Status: approved 2026-09-25, revised v5 (GOLD only, Claude Strategy Review, scalping researched: none ships) · base commit `f0c9ecd` (branch `feature/live-ready`) · preview: https://claude.ai/artifact/JoQGPsktB9Wottx8H8afZs · research scripts and results: [docs/research/strategies-2026-09/](docs/research/strategies-2026-09/README.md)

## Overview

FXCommand's three strategies (EMA Cross, Donchian Breakout, RSI Mean Reversion) lose money after XM's costs. Across 21 symbols they average −0.1 to −0.29R per trade on M15 and H1. The operator's demo record is 27 GOLD M1 trades with 4 winners, a net loss.

This spec replaces them, as the recommended way to trade, with strategies that survive realistic costs. It focuses on GOLD only:

- **GOLD Trend** (`trend_breakout`, GOLD H4): a long-biased Donchian/Turtle breakout.
- **GOLD Reopen Drift** (`session_drift`, GOLD H1): buy at the 01:00 server-time daily reopen and sell about three hours later. It never holds overnight.

A **Claude Strategy Review** runs every weekend. It reviews Paper and Shadow trades, tests hypotheses (including multi-timeframe filters) on history with a trial ledger and a sealed holdout, and submits winners as Challengers. New strategy code arrives only as PRs for the operator to review.

The US500 Index Pullback that was researched earlier is dropped from this build (D40). Its research stays in `docs/research`.

Everything is proven first on a **Paper Account** with a notional balance. Trading the real $50 account is a separate, later decision made by the operator.

Supporting changes:

- **Honest costs:** spread, slippage and swap scaled to price.
- **Pending Entries:** for signals that fall in the daily break.
- **One Magic Number per Assignment:** so two strategies can trade GOLD at once.
- **Cost Check:** blocks cost-dominated setups.
- **Evidence Run:** shows whether an edge exists for an Arena.
- **Graduation scorecard:** compares the Paper record with the backtest.
- **Claude Strategy Review:** a weekly, ledger-counted research loop (see the section below).

## Goals and Non-Goals

### Goals

- Ship the two GOLD strategies above, each backed by evidence measured on the connected feed after spread, slippage and swap.
- Stop cost-dominated setups such as GOLD M1 from starting unless the operator overrides them for that Assignment.
- Make Learning, Shadow Trades, backtests and the Evidence Run charge swap and scale costs with price, so they no longer reward setups that only look profitable.
- Let D1/H4/H1 signals that land in the daily break (00:00–01:00 server) execute at the first tradable tick instead of being rejected.
- Run GOLD Trend and GOLD Reopen Drift concurrently on GOLD.
- Validate on a notional Paper Account with its own limits, and show a per-Arena scorecard.
- Improve the GOLD strategies over time through a weekly Claude review. Improvements must survive the trial-counted evidence bar and a sealed holdout. The target is expected R after costs; win rate is reported but is not the objective.

### Non-Goals

- Maximising backtest profit. About 650 combinations were tested across symbols, plus about 400 GOLD variants. The best of that many is mostly luck, so the target is expected R after costs, out of sample and across periods.
- Making the $50 real account trade these strategies. On current contract specs a minimum-lot GOLD stop risks 33–126% of the account. This spec does not size real trades below what the Risk Gate allows. That remains the operator's decision (ADR 0007 min-lot allowance).
- A micro or cent account. The operator cannot provide one.
- New strategy families beyond these two until they pass the same evidence bar (the weekly review is the route).
- Pyramiding (adding to winners). It failed the every-period rule in research (D46).
- Unsupervised code changes. The reviewer's code arrives only as PRs; merging is the operator's decision.
- Personalised financial advice. The ruin and risk figures here are arithmetic on specs and simulations.

## Research Summary

All figures come from the operator's XM terminal, read-only, 2010–2026. They were produced with FXCommand's own `PaperTrader` fill rules: next-bar-open fills, SL-first on bars that touch both stops, and gap fills.

| Finding | Evidence |
|---|---|
| Old strategies lose after spread | M15 median −0.2 to −0.29R and H1 negative on all 21 symbols |
| Trend portfolios across FX, metals and indices lose once swap is charged | H4/D1 EMA and Turtle portfolios: −0.16 to −0.31R per trade |
| **GOLD H4 Turtle, long-only** | Positive in 8/8 variants. Median by period: 2010–17 +0.106R, 2018–21 +0.046R, 2022–26 +0.731R. About 11 trades/yr. Depends on the regime |
| **GOLD Reopen Drift** (01:00 → 04:00/05:00) | Positive in 12/12 variants in every period (+0.02 to +0.09R). About 250 trades/yr. Entering at 01:15 or 02:00 turns it negative |
| **US500 D1 Index Pullback** | +0.070R/trade after 2× spread and swap. 149 trades. All 36 neighbouring parameter sets positive. Weekend Close cuts it to +0.037R; a 1.5-ATR stop cuts it to +0.028R |
| Intraday GOLD otherwise | Hour-of-day drift from 02:00, previous-day-high breakouts and session breakouts are all ≤ 0 after costs |
| Min-lot risk on $50 | GOLD Reopen ~$16.5 (33%). GOLD Trend ~$63 (126%). US500 ~$22.5 (45%) |
| GOLD Trend upgrades (research data to 2025-09-24) | Ensembles of 20/55/100 breakouts, D1 SMA200 or EMA50/200 regime filters, and an H4 ADX filter: none beat the plain Turtle 55/20 in every period. Stop 2×ATR is the plateau centre |
| Pyramiding (Turtle-style adds) | 2 units: 2010–17 −0.06R; 4 units: negative overall. Rejected |
| Reopen upgrades | The "dip" filter (enter only when H4 is below EMA50) beat the baseline in every research period (+0.059R against +0.032R) but **failed the sealed holdout** (+0.010R against +0.159R). Exit at 05:00 also lost to 04:00 on the holdout. Both rejected |
| GOLD scalping, M15 context → M5 setup → M1 trigger (full history 2020-01 to 2026-06-24 after raising the terminal's bar cap; 2026-06-25 onwards sealed and unused) | 12 designs plus 24 neighbours of the best. Over 6.5 years the M5 pullback and range-reversion signals are ~0 even before costs; the earlier +0.05R was a short-sample effect. An M1 trigger made every design worse. The only positive design, the NY opening-range breakout (30-min range from 16:30 server, long, M15 trend), made +0.039R after XM costs but −0.013R in 2024–26. 0 of 24 neighbours (range 15–60 min, target 1.5–3×, filter on/off) are positive in every period, so it is a narrow peak, not a plateau. No finalist; the holdout stays sealed |
| **Sealed holdout, 2025-09-25 to 2026-09-24, scored once** | Turtle 55/20: 9 trades, +0.369R each. Reopen 01→04: 257 trades, +0.159R each, 58% win rate, max drawdown 6.9R |

Costs used:

- **GOLD spread:** $0.57, or $0.70 at the reopen, measured from ticks. It moves from $0.70 at 01:00 to $0.57 by 01:15.
- **Slippage:** $0.05.
- **GOLD swap:** long −86.84 points/night (about $0.87/oz), short +19.79.
- **Scaling:** every cost is scaled by close ÷ today's price.

## Technical Design

### Strategies (`backend/fxcommand/strategies/catalog.py`)

New catalog entries only. The existing strategies' behaviour and golden rows are unchanged. Each new strategy is vectorised (ADR 0005) and gets golden rows.

| Key | Default Arena | Rule (defaults) | Params (optimizer search bounds) |
|---|---|---|---|
| `trend_breakout` | GOLD H4 | Long when close > highest high of the previous `entry` bars. Exit when close < lowest low of `exit` bars. Stop `sl_atr`×ATR(`atr_period`). Short mirror only when `allow_short` | `entry` 55 (40–100), `exit` 20 (10–50), `atr_period` 20, `sl_atr` 2.0 (2–3), `allow_short` false |
| `session_drift` | GOLD H1 | Long at `entry_hour` (server) on Mon–Fri. Exit at the `exit_hour` bar open. Stop `sl_atr`×ATR(`atr_period`). Declares `fill_window_s` | `entry_hour` 1 (fixed), `exit_hour` 4 (3–5), `atr_period` 24, `sl_atr` 1.5 (1–2), `fill_window_s` 300 |

- Parameter names `atr_period` and `sl_atr` are required, because the live path and preflight read them.
- Search bounds are optimizer metadata, separate from the Param min/max the editor enforces.
- `session_drift` hours are server time, anchored to the broker's daily reopen. XM's server clock follows New York daylight saving, so the reopen stays at 01:00.
- `session_drift` needs enough warm-up for ATR(24) on H1 only. Live fetches at least `lookback + WARMUP_BARS` bars.
- Strategies accept optional multi-timeframe filter inputs (the last closed bar of a higher timeframe, aligned with `merge_asof` on close time, never a forming bar). They are off by default, and the reviewer may propose them as Challengers.

### Cost model (`learning/paper.py` `Costs`, new `learning/costs.py`)

- `CostModel(spread, slippage, swap_long, swap_short, swap_mode, rollover3days, ref_price, version)` is built from live `symbol_info`.
- Per bar, spread and slippage are scaled by `close / ref_price`.
- Swap per night is computed by `swap_mode`:
  - points: × point
  - money per lot: ÷ value-per-price
  - It is scaled by `entry / ref_price`.
- The swap is charged for every server-day rollover between open and close, three times on `rollover3days`.
- One function, `swap_for(position, from_ts, to_ts)`, is shared by `PaperTrader`, `PaperBook`, the Evidence Run and Shadow Trades.
- `version` is part of Evidence keys.

### Pending Entries and deferred exits (engine)

- **Creation:** when an entry or strategy-exit signal arrives while the market is closed, the Trading Window is shut or the quote is stale, the engine persists a Pending Entry. It does not reject.
- **Table and key:** `pending_entries`, keyed by `(session_id, symbol, timeframe, signal_bar_ts, candidate_key)`. Never keyed by assignment id.
- **Sending:** only through `SessionManager._enter`, re-gated against the first tradable tick. This keeps every guard: the one `broker.run` gate+send, `_kill_seq`, never opening while a close failed, one position per Assignment, and Order Outcomes. An `uncertain` entry is never retried.
- **Retries:** only transient rejects are retried: `outside_window`, market closed, spread, stale quote. The first reject is journaled once. Hard rejects end the Pending Entry.
- **Expiry:** after the strategy's `fill_window_s`. The default is the next bar close; `session_drift` uses 300 s. Expiry and fill delay/spread are written to the Journal and to the Learning Signal record.
- **Cancelled by:** Stop, Kill Switch, Auto-stop, a Pinned Login change, or a Champion change for that Arena.
- **Restart:** a Pending Entry survives Interrupted. After a restart it is sent only if the Session is restarted before expiry.
- **Pause:** a paused Session never sends.
- **Deferred exits:** strategy exits that fall in the daily break use the same path. Server-side SL/TP stay in place throughout (Broker mode), and the Paper book checks them on M1 bars.
- **Backtests:** entries check the window at the next tradable time, using the shared helper `next_tradable(ts, window, hours)`.

### One Magic Number per Assignment (supersedes ADR 0003, new ADR 0009)

- Each Assignment has its own never-reused Magic Number. The magic is keyed by `(session_id, symbol, strategy, timeframe)`, so it survives edits even though assignment rows are recreated.
- Position ownership is `(magic, symbol)` per Assignment. The account is hedging (ADR 0006), so two GOLD positions can coexist.
- Uniqueness: an Arena `(symbol, timeframe)` appears at most once among running Assignments, so Learning records keyed by Arena never collide.
- **Migration:** the Session's existing magic moves to its first Assignment, and the others get new magics.
- **Affected:** the Kill Switch (any FXCommand magic), Orphan adoption, `PaperRouter` routing, Live Caps and global position counts, trade history (`TradeRow.magic`) and comments (`fxc s<id> <strategy>`).

### Paper Account (amends ADR 0007)

- One notional balance, `paper_account.start_balance`, set on the Account page (e.g. $5,000). Equity = start + realized Paper P&L + open Paper P&L, including swap.
- A Paper `AccountInfo` is built outside `gate_and_send` and never written into `self._account`. The real Equity Floor, day-start and snapshots are untouched.
- Paper day-start is recorded at the server-day roll under `day_start:paper`.
- Risk %, daily-loss and global limits apply to the Paper pool. A Paper limit auto-stops only Paper Sessions.
- **Reset:**
  - It needs typed confirmation.
  - It is refused while any Paper Session is active or any Paper position is open.
  - Rows are archived with an epoch id and never deleted.
  - Paper tickets come from a counter that only goes down, so they are never reused.
- **Catch-up after downtime:** pages M1 bars from `checked_to`, falling back to H1 and then D1 when M1 history is short.
- **Statistics:** Overview, History and Strategies show Paper Account and real account separately.

### Cost Check

- Cost is (spread + slippage) as R per trade for each Assignment, using the strategy's typical stop:
  - Spread comes from recent ticks, or the median of the bar spread column calibrated to ticks. It is never taken from a single tick at the daily break.
- Start is blocked above `cost_check_max_r` (Settings, default 0.15R) unless an override for `(session_id, symbol)` is set. The override is journaled.
- Enforced in `SessionManager._start` and again when a Promotion is applied. Auto-resume journals a blocked Session and skips it. The API and MCP cannot bypass the check.
- Swap per trade is shown next to it (nights held come from Evidence or Shadow Trades) but does not block. The Evidence judges whether the edge survives swap.
- Expected: GOLD M1 ~0.3–0.5R (blocked), GOLD Reopen ~0.05R, GOLD Trend ~0.01R + ~0.1–0.2R swap (shown).

### Evidence Run and scorecard

- A read-only backtest of an Arena on the connected feed's full history, using the current cost model at 2× spread.
- Keyed by `(symbol, timeframe, candidate_key, exit_rules_hash, weekend_close, window_hash, cost_version)`. The editor shows "evidence does not match these settings" when an Assignment differs. Runs default to no breakeven and no trailing stop.
- Stores trades, mean R, SQN, max drawdown, and mean R per period (2010–17, 2018–21, 2022–26 or thirds), plus bars received and the date.
- **Scheduling:**
  - One `broker.run` per Arena, in chunks of at most 10k bars, each with a short timeout.
  - The backtest runs in the learning process pool on a low-priority queue.
  - A run is refused while an entry is in flight.
- **Storage:** the local DB only (`backend/data` is gitignored). MCP gets a read-only `get_evidence` and nothing that starts runs, overrides or resets.
- **Scorecard, per Arena:** the Paper record (trades, mean R) against the backtest's 90% band, plus real-account min-lot risk % at current ATR. It is advisory and never blocks.

### Learning

- The swap-aware, price-scaled cost model and `next_tradable` window timing are used everywhere.
- Retention keeps at least the last 200 closed Shadow Trades per Candidate per Arena, whatever their age. The 180-day rule applies only beyond that.
- Candidate generation stays within the Champion's family. Old Arenas never draw the new families, and the new families never draw old ones.
- The noise and planted-edge tests are re-pinned. Guardrail constants are unchanged. Auto-promotion is never allowed on a live account.

### Claude Strategy Review (weekly research loop)

**Trigger.** A Claude Code scheduled task runs every Saturday, while GOLD is closed, in this repository. The operator can also run it on demand. The protocol lives in a project skill, `.claude/skills/gold-review/SKILL.md`.

**Loop.**
1. Read the week's Paper, Shadow and live trades, the Journal (Risk Gate rejections, Pending Entry expiries, fill delays and spreads), Evidence and scorecards. These come through the read-only MCP tools.
2. Classify the mistakes:
   - losers that hit the stop right after entry;
   - entries missed by the fill window;
   - trades that went against the higher-timeframe context;
   - costs above expectation;
   - divergence between Paper and backtest.
3. Form hypotheses: parameter changes, multi-timeframe filters, session or day filters, exits. Each one is a rule that can be expressed as a Candidate or as new strategy code.
4. Test each hypothesis with the research CLI `uv run fxcommand-research` on the **research snapshot**. The app exports this read-only bar snapshot per Arena **with the sealed holdout cut out**. Every run is appended to the **trial ledger**, whatever its result.
5. A hypothesis becomes a finalist only if it beats the current Champion in **every** research period, net of costs and swap, and after the selection penalty for the Arena's total ledger trials (`DEFLATE_K · sqrt(2 ln N)`). Its neighbouring parameters must also stay positive.
6. Each finalist is scored **once** on the sealed holdout, and the use is recorded in the ledger. If it fails there, it is rejected for good.
7. Output:
   - A finalist that fits an existing strategy is submitted with `submit_challenger`. It then runs Shadow Trades and Paper under the existing Guardrails, and auto-promotion never happens on a live account.
   - A finalist that needs new code is written on a branch `review/<date>-<slug>` with tests, and a PR is opened. It is never merged by Claude.
   - A review report goes out through `submit_review`.

**Arenas under review.**

- **GOLD H4:** GOLD Trend.
- **GOLD H1:** GOLD Reopen Drift.
- **GOLD M5, the scalping research track:** kept separate from the H4/H1 strategies.
  - Context comes from M15, and M1 can serve as an entry trigger.
  - Nothing trades on this Arena until a scalping design passes the full bar: it must beat costs in every research period, clear the ledger penalty, and survive the sealed holdout.
  - A design that passes ships as its own strategy with its own Assignment and Magic Number, and it goes through the Cost Check like any other.
  - Scalping Arenas use a 3-month holdout, because the M1/M5 history is short.

**Sealed holdout.** The most recent 12 months per Arena (3 months for M1/M5 Arenas). It rolls forward monthly. Data that leaves the holdout becomes research data. The ledger records every holdout evaluation, and the holdout is refreshed only by rolling.

**Trial ledger.** A table `research_trials(arena, hypothesis, params_hash, data_range, result, holdout_used, ts)`. Its per-Arena count feeds the selection penalty of both the reviewer and the Walk-forward Guardrail for reviewer-submitted Challengers.

**Objective.** Expected R per trade after costs and swap, out of sample. Win rate, drawdown and trades per year are reported. A change that only raises win rate while lowering expected R is rejected.

**Reports.** Stored in the local DB (`reviews` table) and shown on a new **Reviews** page: mistakes found, hypotheses tested (with ledger counts), finalists, holdout results and actions taken. A short summary goes to Telegram. PR descriptions contain code rationale only, with no P&L and no account data (the repo is public).

**Authority.**

- **May:**
  - read everything read-only;
  - export and read the research snapshot;
  - run research backtests locally;
  - submit Challengers and reviews;
  - open PRs.
- **May not:**
  - start, stop or edit Sessions;
  - promote a Challenger;
  - enable live trading;
  - change risk, Live Caps or the Equity Floor;
  - override the Cost Check;
  - reset the Paper Account;
  - merge code;
  - call any order method.

### Weekend Close

- Stays a per-Session toggle, off by default for Sessions with D1/H4 Assignments.
- When it is on for such a Session, the editor warns that it cuts the tested edge. For a GOLD Trend Arena the warning shows that Arena's Evidence with and without Weekend Close.
- The backtest and Evidence model the Session's setting.

### Risk Profiles

- **Gold Reopen:** a 100-point spread cap. The reopen spread is ~70 points, above the current Gold profile's 60.

## API and MCP

- `GET /api/evidence?symbol&timeframe&strategy`, `POST /api/evidence/run` (UI only), `GET /api/scorecard`.
- `GET/PUT /api/paper-account`. `POST /api/paper-account/reset` with a typed confirmation.
- `GET /api/sessions/{id}/cost-check`. The override is part of the Session update payload and is journaled.
- `GET /api/research/snapshot?arena` (bars with the sealed holdout cut out), `GET/POST /api/research/trials` (the ledger), `POST /api/research/holdout` (a one-time finalist evaluation, recorded in the ledger).
- `GET /api/reviews`, `POST /api/reviews`, `POST /api/learning/challengers` (reviewer submission; the same Guardrails apply).
- MCP:
  - read-only `get_evidence`, `get_research_snapshot` and `get_trials`;
  - two narrow write tools, `submit_review` and `submit_challenger`. They write only to review, ledger and learning tables.
  - No tools for Session control changes, overrides, Paper reset, Evidence runs, live enabling, risk or promotion.

## UI

- **Session editor:**
  - Several Assignments per Symbol, as long as their timeframes differ.
  - An Evidence badge per Assignment ("+0.07R · 149 trades", "no evidence", "does not match settings").
  - Cost Check cost and swap per Assignment, with an override checkbox.
  - The Weekend Close warning for D1/H4.
- **Start dialog:** the Cost Check result, and a block when a check fails without an override.
- **Strategies page:** the Evidence table and the "Run evidence" action.
- **Account page:** the Paper Account (balance, equity, reset) and the scorecard.
- **Overview, History, Strategies:** statistics split between Paper Account and real account.
- **Reviews page:** weekly reports, the ledger count per Arena, finalists with their holdout result, and links to Challengers and PRs.

## Error Handling and Edge Cases

- **Reopen gap through the stop:** stop distance is priced from the fill tick. When the gap puts the stop past the market, the Risk Gate rejects the entry (hard reject).
- **Broker holiday or late reopen:** the Pending Entry expires after its fill window. The expiry is journaled and recorded in the Signal.
- **Opposite signal while a Pending Entry waits:** the pending one is cancelled, and the new signal follows the normal reverse rules.
- **Terminal logged into another account:** Sessions go Interrupted (Pinned Login) and Pending Entries are cancelled.
- **Evidence Run fetch timeout:** that Arena is marked incomplete with the number of bars received. The engine is never blocked.
- **Learning errors:** swallowed and journaled once (`_learn`). Learning never stops trading.

## Security Considerations

- **Public repository:** no account numbers, balances, credentials or feed-derived Evidence in committed files. Evidence lives in the local DB. This spec's research figures describe market data and hypothetical sizing only.
- **Local API:** it has no authentication and binds to 127.0.0.1 (unchanged). New write endpoints (Paper reset, Cost Check override, Evidence run) are not exposed to MCP.
- **Reviewer boundary:** the reviewer's only write paths are `submit_review` and `submit_challenger` (review, ledger and learning tables) and git branches or PRs. Claude never merges. CLAUDE.md gains these rules.
- **Orders:** no test, script or agent sends an order through `Mt5Broker`. The Evidence Run and the MT5 smoke probe are read-only, with order methods tripwired.

## Testing Strategy

- **SimBroker:**
  - A daily break until 01:00 and a wider reopen spread.
  - Swap fields and ≥ 600 days of history.
  - GOLD specs.
  - The Gold Reopen Risk Profile.
- **pytest:**
  - Strategy golden rows.
  - Cost model: scaling, swap modes, triple day.
  - `next_tradable`.
  - Pending Entry states: restart, Pause, Kill Switch, login change, Champion change, fill-window expiry, transient against hard rejects.
  - Magic per Assignment: migration, two GOLD positions, Orphans, Kill Switch, Paper routing.
  - Paper Account: limits, isolation from the real account, reset rules, ticket counter, swap, catch-up.
  - Cost Check enforcement via the API, MCP, auto-resume and promotion.
  - Evidence keys and scheduling.
  - Retention and family-scoped candidates.
  - Re-pinned noise and planted-edge tests.
  - Research snapshot never contains holdout bars.
  - Every CLI run appends to the ledger.
  - A holdout evaluation is recorded and can't be repeated silently.
  - The ledger count raises the penalty.
  - MCP `submit_challenger` cannot promote or touch Sessions.
- **E2E:**
  - Two GOLD Assignments in one Session.
  - Evidence badges.
  - Cost Check block and override.
  - The Paper Account.
  - The stats split.
  - The scorecard.
  - The Reviews page.
- **Exploratory:** a `playwright-cli` pass over every page, including phone width.
- **MT5 read-only smoke:**
  - When the H1 and D1 bars appear after the reopen.
  - GOLD spread per minute from 01:00 to 01:15.
  - A Paper Session with order methods tripwired.

## Decisions Log

| ID | Topic | Decision | Rationale | Source | Date |
|---|---|---|---|---|---|
| D1 | Account | Target the existing $50 XM Standard demo; no micro/cent account | Operator cannot provide a micro account | Interview | 2026-09-25 |
| D2 | Goal | Prove on Paper first with notional sizing; real trading is a later operator decision | No setup with an edge fits $50 at survivable risk | Interview | 2026-09-25 |
| D3 | Families | Evidence-only: ship only families that survive full costs; make Learning swap-aware | Stops shipping edgeless strategies | Interview | 2026-09-25 |
| D4 | Old strategies | Keep them; add a blocking Cost Check with a per-Assignment override | History and golden fixture; stops GOLD-M1-style bleeding | Interview | 2026-09-25 |
| D5 | D1 timing | Deferred Pending Entry, re-gated at the first tradable tick | Matches the backtest's next-open fill | Interview | 2026-09-25 |
| D6 | Weekend | Per-Session Weekend Close, off by default for D1/H4, with a warning; the backtest models it | It halves the US500 edge | Interview | 2026-09-25 |
| D7 | Arenas | Assignable anywhere; Evidence table and "no evidence" badge | Informs without a whitelist | Interview | 2026-09-25 |
| D8 | Paper money | One Paper Account notional balance shared by Paper Sessions | Tests portfolio limits realistically | Interview | 2026-09-25 |
| D9 | Graduation | Advisory per-Arena scorecard with real-account risk math | The operator decides on real money | Interview | 2026-09-25 |
| D10 | Learning | Narrow, swap-aware optimizer bounds; Guardrails unchanged | Flat plateaus; tuning mostly fits noise | Interview | 2026-09-25 |
| D11 | Evidence source | In-app read-only Evidence Run | Works on any broker; stays current | Interview | 2026-09-25 |
| D12 | Exits | Exits that fall in the daily break are deferred like entries | Market is shut | Interview | 2026-09-25 |
| D13 | Pending scope | Pending Entries apply on any timeframe when the market or window is shut | One mechanism | Interview | 2026-09-25 |
| D14 | Cost threshold | 0.15R default, set in Settings | Blocks M1/M15 on XM | Interview | 2026-09-25 |
| D15 | Swap model | Mode-aware swap in backtests, Shadow Trades, the Evidence Run and Paper | Swap reverses many trend results | Interview | 2026-09-25 |
| D16 | Stats split | Separate Paper Account and real account statistics | Removes mixed stats | Interview | 2026-09-25 |
| D17 | Invariants | Old golden rows, Guardrail constants and the no-MT5-orders rule unchanged | Existing guarantees | Interview | 2026-09-25 |
| D18 | Backtest window | Check the window at the next tradable time with a helper shared with live | Otherwise backtests open 0 D1 trades | Red Team | 2026-09-25 |
| D19 | Paper pool | Separate Paper AccountInfo, day-start and limits, never written to the real account; amend ADR 0007 and CLAUDE.md | A $50 day-loss cap would stop Paper after one loss | Red Team | 2026-09-25 |
| D20 | Pending lifecycle | Persisted and keyed by Arena and bar; cancel events defined; sent only via _enter; transient retries only; journal once | Keeps every existing entry guard | Red Team | 2026-09-25 |
| D21 | Paper reset | Refused while Paper is active or open; archive with epoch id; tickets only go down | Prevents ticket reuse and orphaned trades | Red Team | 2026-09-25 |
| D22 | Retention | Keep at least 200 closed Shadow Trades per Candidate per Arena | 180 days holds only a few D1/H4 trades | Red Team | 2026-09-25 |
| D23 | Evidence key | Key by Arena, candidate, exit rules, weekend, window and cost version; mismatch warning | Stops showing evidence for untested settings | Red Team | 2026-09-25 |
| D24 | Evidence scheduling | Per-Arena chunked fetches, process pool, refused while an entry is in flight | BrokerThread contention could create Orphans | Red Team | 2026-09-25 |
| D25 | Search scope | Family-scoped candidates; separate search bounds; re-pin tests | Stops cross-family promotions | Red Team | 2026-09-25 |
| D26 | Cost inputs | Spread from ticks or calibrated bar median; enforced in _start and at promotion; auto-resume skips | Live spread widens at the break; UI-only check is bypassable | Red Team | 2026-09-25 |
| D27 | Sim and probe | SimBroker daily break, swap and history; Index profile; read-only timing probe | D1/H4 paths otherwise untestable; US500's 80-point spread | Red Team | 2026-09-25 |
| D28 | Paper swap | Rollover-based swap with a shared function; paged M1 catch-up with H1/D1 fallback | No double-count across restarts | Red Team | 2026-09-25 |
| D29 | Exposure | Evidence in the local DB only; MCP read-only get_evidence | Public repo; no-auth API | Red Team | 2026-09-25 |
| D30 | Focus | GOLD is the primary instrument | Operator direction | Interview | 2026-09-25 |
| D31 | Strategy set | GOLD Trend (H4) and GOLD Reopen Drift (H1) primary; US500 Index Pullback secondary | Only GOLD families positive across all variants; the three are uncorrelated | Interview | 2026-09-25 |
| D32 | Reopen fill | Send at the first tick after 01:00; 5-minute fill window; Gold Reopen profile with a 100-point cap; journal delay and spread | The edge is gone by 01:15; reopen spread ~70 points | Interview | 2026-09-25 |
| D33 | Same symbol | One Magic Number per Assignment; supersede ADR 0003 | Two GOLD strategies at once on a hedging account | Interview | 2026-09-25 |
| D34 | Cost scaling | Spread, slippage and swap scale with price in multi-year backtests | Fixed dollar costs overstate early years 2.5–3.5× | Interview | 2026-09-25 |
| D35 | Time anchor | Session Drift hours are server time from the daily reopen | Survives daylight-saving shifts | Interview | 2026-09-25 |
| D36 | Trend direction | GOLD Trend long-only by default; allow_short param | Long-only median positive in all periods; long+short negative in 2018–21 | Interview | 2026-09-25 |
| D37 | Magic identity | Magic keyed by (Session, Symbol, Strategy, Timeframe), stable across edits; Arena unique among running Assignments | Assignment ids are recreated on edit; Learning keys by Arena | Interview | 2026-09-25 |
| D38 | Swap in Cost Check | The Cost Check blocks on spread + slippage only; swap is shown, and the Evidence judges it | GOLD Trend pays swap yet stays positive | Interview | 2026-09-25 |
| D39 | Fill window | Pending Entry expiry is set per strategy (300 s for Reopen, next bar close otherwise) | Edges decay at different speeds | Interview | 2026-09-25 |
| D40 | Scope | GOLD only: build GOLD Trend and GOLD Reopen Drift; drop US500 Index Pullback from this build (supersedes the secondary part of D31) | Operator wants GOLD focus | Interview | 2026-09-25 |
| D41 | Self-learning | Add a Claude Strategy Review loop that reviews mistakes and proposes upgrades, including multi-timeframe filters | Operator direction | Interview | 2026-09-25 |
| D42 | Reviewer authority | Challengers via MCP automatically; new code only as PRs the operator merges; never live, risk, Sessions or orders | Learning speed without unreviewed engine code | Interview | 2026-09-25 |
| D43 | Review trigger | Weekly scheduled task on Saturday, plus on demand | Enough new trades; no interference with trading | Interview | 2026-09-25 |
| D44 | Trial guard | Trial ledger with a growing selection penalty, plus a sealed rolling 12-month holdout scored once per finalist; failure is final | Weekly mining would otherwise find luck | Interview | 2026-09-25 |
| D45 | Review reports | Local DB and in-app Reviews page, plus a Telegram summary; PRs carry code rationale only | Public repo | Interview | 2026-09-25 |
| D46 | Pyramiding | Build only if research proves it; research rejected it (fails every-period rule) | 2 units negative in 2010–17; 4 units negative overall | Interview | 2026-09-25 |
| D47 | Reviewer I/O | Research CLI on a holdout-free snapshot; MCP submit_review and submit_challenger only | Narrow write surface | Interview | 2026-09-25 |
| D48 | Objective | Expected R after costs; win rate reported, not optimised | Win rate can rise while profit falls | Interview | 2026-09-25 |
| D49 | Upgrade results | Keep Turtle 55/20 and Reopen 01→04 baselines; reject ensembles, regime filters, the Reopen dip filter (failed holdout) and exit at 05:00 | Every-period rule and sealed holdout | Interview | 2026-09-25 |
| D50 | Volatility sizing | No extra work: stop = k×ATR with risk-% sizing already keeps risk constant | Already inherent in the Risk Gate | Interview | 2026-09-25 |
| D51 | Scalping | GOLD scalping (M1/M5/M15 multi-timeframe) is a research track in the weekly Claude review, on its own Arena, separate from the H4/H1 strategies. No scalper engine work until a design passes | 10 designs lose after costs; the signal is smaller than the spread | Interview | 2026-09-26 |
| D52 | History depth | The operator sets MT5 "Max bars in chart" to Unlimited. M1/M5 are then re-pulled read-only and the scalping research is re-run over years | 100k-bar cap gives only 3 months of M1 | Interview | 2026-09-26 |
| D53 | Scalping result | No GOLD scalping strategy ships. The NY opening-range breakout stays a watched hypothesis in the weekly review; the sealed scalping holdout (2026-06-25 onwards) is still unused | Fails every-period rule and neighbourhood robustness at XM costs | Interview | 2026-09-26 |
| D54 | Reviewer tool boundary | The weekly review's scheduled task gets an allowed-tools list without the operator MCP tools (start/pause/resume/stop Session, Kill Switch, run learning), and its Bash is limited to the research CLI; the MCP server keeps its full tool set for the operator | The local HTTP API has no authentication, so hiding MCP tools alone would not stop a free Bash call | Interview | 2026-09-27 |
| D55 | Reviewer tests and git | The scheduled review may also run pytest and the review-branch git/gh steps (extends D54). Hypothesis scripts cannot import MetaTrader5; the allow-list stops direct operator actions but is not a sandbox for code the reviewer writes, so its PRs are reviewed before merge | The reviewer must test and propose code; running its PR tests elsewhere would slow the loop | Interview | 2026-09-27 |

## Dependency Graph & Implementation Order

```
1 Pure core ──┬──> 4 Learning ──> 6 Evidence Run ─┐
              └──> 3 Magic per Assignment ─┐       ├──> 7 API + MCP ──> 8 UI ──> 9 Claude Review ──> 10 Verify
2 SimBroker ──┬──> 3                       ├──> 5 Engine ┘
              └──> Store tables ───────────┘
```

1. **Pure core:** `CostModel` (price-scaled, swap modes, version), `next_tradable` helper, the `trend_breakout` and `session_drift` strategies with golden rows, Cost Check math.
2. **SimBroker:**
   - Daily break and reopen spread.
   - Swap fields and ≥ 600 days of history.
   - GOLD specs.
   - The Gold Reopen Risk Profile.
3. **Magic per Assignment** (ADR 0009 supersedes 0003):
   - Stable magic identity and the store migration.
   - Ownership, Orphans, Kill Switch, Paper routing, Live Caps counts.
4. **Learning:**
   - `PaperTrader` uses the `CostModel` and the window fix.
   - Retention and family-scoped candidates with search bounds.
   - Re-pinned noise and planted-edge tests.
5. **Engine:**
   - Pending Entries and deferred exits with per-strategy fill windows.
   - Paper Account: pool, day-start, reset, ticket counter, swap, catch-up.
   - Cost Check enforcement in `_start` and at promotion.
   - Docs: ADR 0008 (Pending Entries and Paper Account); amend ADR 0007, `CONTEXT.md` and `CLAUDE.md`.
6. **Evidence Run and scorecard:** table, chunked read-only fetches, per-period results, band and risk math.
7. **API and MCP:** evidence, cost-check, paper-account, scorecard, research snapshot and ledger, and reviews endpoints; MCP read-only tools plus `submit_review` and `submit_challenger`.
8. **UI:**
   - Session editor with several Assignments per Symbol, Evidence badges, Cost Check and the weekend warning.
   - Strategies Evidence table.
   - Paper Account and scorecard on the Account page.
   - Stats split.
   - Reviews page.
9. **Claude Strategy Review:**
   - `fxcommand-research` CLI with snapshot, ledger and one-shot holdout.
   - The `gold-review` skill with the protocol and authority limits.
   - A weekly scheduled task.
   - CLAUDE.md rules.
10. **Verify:** `uv run pytest`, `npm run build`, E2E, the `playwright-cli` pass, and the read-only `pytest -m mt5` with the reopen timing and spread probe.

## Implementation Checklist

### 1. Pure core
- [x] `learning/costs.py`: `CostModel` with price-scaled spread, slippage and swap (`swap_mode` points / money / interest; `swap_rollover3days`; `version`)
- [x] `CostModel.swap_for(side, entry_price, open_ts, close_ts)` with MT5-style rollovers (triple day). It is wired into `PaperTrader`, `PaperBook`, the Evidence Run and Shadow Trades in milestones 4–6
- [x] `risk/window.py`: `TradingHours` and `next_tradable(ts, window, hours)`
- [x] Strategies `trend_breakout` and `session_drift` (declares `fill_window_s`), in `strategies/gold.py`, pinned by `tests/fixtures/golden_gold_signals.json`. Old golden rows unchanged. On real XM history they reproduce the research: GOLD Trend's sealed year matches exactly (9 trades, +0.369R). Reopen's research mean matches exactly (+0.032R). Its sealed year has ~4% fewer trades (247 against 257), because days after an irregular closure are skipped
- [x] `strategies/mtf.py` `align_closed` (last closed higher-timeframe bar, never a forming one). Research wiring came with the reviewer (milestone 9: `--script` with `CONTEXT` + `prepare`); the live engine gets other-timeframe bars with the first multi-timeframe filter that passes, as new code in its PR
- [x] `risk/costcheck.py` `estimate_cost`: (spread + 2 × slippage) ÷ stop; swap reported, not blocking

### 2. SimBroker
- [x] Daily break until 01:00 and a wider reopen spread. GOLD is shut 23:57–01:00 Mon–Fri: no bars, orders/modify/close answer market closed (10018), stops wait for the reopen and fill at the gap, the last quote ages. 70 points for the first 15 minutes after 01:00. Other Symbols' price paths are unchanged (same random draws). Behaviour change for all Symbols: orders at a weekend boundary (e.g. on the Friday 23:59 bar close) are now refused as market closed. A stop move refused in the break is retried every pass but journaled once
- [x] Swap fields on `SymbolInfo` (`swap_long`, `swap_short`, `swap_mode`, `swap_rollover3days`), mapped in `Mt5Broker`, in `fake_mt5` and the sim; `CostModel.from_symbol` reads them. The sim does not charge swap on its own positions; the Paper book does from milestone 5
- [x] At least 600 days of history: `deep_history_days` adds H1 history before the M1 history for H1/H4/D1 (`FXC_SIM_DEEP_DAYS`, 600 when the app runs, 0 in unit tests). GOLD specs: XM trading hours, reopen spread and swaps; base spread and price unchanged so existing GOLD tests keep their meaning. The golden fixtures are generated on `LEGACY_SYMBOLS`, so spec changes never move their inputs
- [x] Gold Reopen Risk Profile (100-point cap, no breakeven or trailing). Seeded once into existing databases too; a profile the operator deletes is not brought back

### 3. Magic Number per Assignment
- [x] ADR 0009 superseding ADR 0003; update `CONTEXT.md` and `CLAUDE.md`
- [x] Magic identity keyed by `(session_id, symbol, strategy, timeframe)` in `magic_identities`, stable across edits, never reused (a deleted Session's identities stay reserved under its negated id, because SQLite reuses ids); `AssignmentRow.magic`; existing Sessions migrated (their magic moves to the first Assignment). A position whose Strategy changed underneath it (stop leaving positions, edit, restart) stays with the Assignment in its Arena
- [x] An Arena is unique within a Session and among running Sessions; the same Symbol is allowed on different timeframes. Learning slots and pending changes are keyed `(session_id, symbol, timeframe)`; the `/learning/slots/...` routes take an optional `?timeframe=` and refuse an ambiguous call. The Session editor still refuses a Symbol twice: milestone 8
- [x] Ownership through one rule (`SessionManager._owner`), Orphan adoption, Kill Switch, stop-and-close, Weekend Close, `PaperRouter` (every magic of every Paper Session, refreshed after a Promotion), Live Caps and global counts, trade history (`TradeRow.magic` = the Assignment's)
- [x] Tests: two GOLD positions at once, migration with legacy positions (no duplicate entry), Kill Switch, Orphans, Paper routing with the real Account's order method watched (edit + restart included), strategy edit with a position left open, no magic reuse. The Paper check on the real feed is still owed while the market is closed

### 4. Learning
- [x] `PaperTrader` uses `CostModel` and `next_tradable`: spread and slippage at the bar's price, swap per MT5 rollover (three nights on Wednesday), and the Trading Window judged when the entry fills (`EntryGate`: open within the bar, or the Strategy's shorter fill window). Shadow Trades and Optimizer Runs price with a typical spread: the median of recent fresh quotes outside 23:55–00:10, never a quote from the daily break. Until milestone 5 the live engine still judges the window at the signal, so Shadow Trades can take an entry live refuses (e.g. an H4 bar at 00:00). On real XM GOLD history the app reproduces the research: Reopen Drift exactly (3075 trades +0.0324R, sealed 247 +0.1475R); GOLD Trend exactly without swap and within 0.003R (research) / 0.012R (sealed year: 9 trades +0.357R vs +0.369R) with it, because the research counted calendar days (2016 nights) where MT5 charges 2002
- [x] Retention keeps at least 200 closed Shadow Trades per Candidate per Arena (`KEEP_SHADOWS`); the 180-day rule applies only beyond them; open ones are never deleted
- [x] Family-scoped candidate generation with optimizer search bounds (`Strategy.family`, `Param.search_min/search_max`). Done early in milestone 1 so the new strategies never leak into the classic Arenas
- [x] Re-pin the noise and planted-edge tests; Guardrail constants unchanged. Both pass unchanged on the new pricing (nothing to re-pin); the sim still charges no swap on its own positions, so an overnight live trade in the sim can differ from its Shadow Trade by the swap

### 5. Engine
- [x] `pending_entries` table and lifecycle: transient retries (outside window, stale quote, spread, market closed) on every engine pass, per-strategy fill window from the observed Next Tradable Time (`fill_limit`, shared with `EntryGate`), cancel events (Stop, Kill Switch, Auto-stop, Pinned Login change, Champion change, Weekend Close), survives Interrupted, journal once, outcome on the Signal record. In the sim, GOLD Reopen fills at 01:01 and its live trades match its Shadow Trades in Broker and Paper mode; on the Gold profile (60 points) the 70-point reopen spread outlasts the 5-minute window and the entry expires
- [x] Deferred exits for the daily break: a strategy close the market refuses waits as a pending exit (no alert; no new side meanwhile). The Paper book refuses such closes while the quote is stale; a Paper stop / Kill Switch still settles
- [x] Paper Account: pool (`paper_account.start_balance`, default 5,000), `day_start:paper`, limits that stop only Paper Sessions, typed reset (`RESET PAPER`) refused while active or open, archive epoch, monotonic tickets, swap, paged catch-up (M1, then H1/D1 for older gaps). The Paper `AccountInfo` is used for sizing only and never becomes the engine's account
- [x] Cost Check enforced in `_start` and at promotion; override per `(session_id, symbol, timeframe)` (the timeframe is added because of ADR 0009: a GOLD H1 override must not cover a GOLD M1 scalper), journaled; auto-resume skips. Priced from `SpreadBook` (recent good quotes, persisted), never from a quote in the break; with nothing known and the market shut it refuses to start. Limit `cost_check_max_r` in Settings (default 0.15R)
- [x] Weekend Close default off for D1/H4 Sessions, with the editor warning data (`weekend_close_warning` on the Session; the with/without Evidence comparison comes with milestone 6)
- [x] ADR 0008 (Pending Entries and Paper Account); amend ADR 0007, `CONTEXT.md` and `CLAUDE.md`

### 6. Evidence Run and scorecard
- [x] Evidence table (`evidence_runs`) keyed by `(symbol, tf, candidate_key, exit_rules_hash, weekend_close, window_hash, cost_version)` through one function (`EvidenceSpec.key`) used both to run and to look up; lookups answer match / running / mismatch / none, and an H4/D1 Assignment with Weekend Close on also gets the Evidence without it. Weekend Close is now modelled in Evidence Runs, Optimizer Runs and Shadow Trades (`WeekendClose`: flat at the close of the bar holding Friday's close time, no fill inside the span; off changes nothing)
- [x] Chunked read-only fetches: `closed_bars` gained an `offset` (MT5 `copy_rates_from_pos` start position; checked read-only against the real terminal), pages of ≤ 10k bars, each its own 10 s broker call (a 10k page of GOLD M1/M5/H1 read in ≤ 0.6 s on the real terminal, down to the 400k cap; a page that times out still holds the broker thread until it returns), newest first, de-duplicated by time, capped at the most recent 400k bars. Pages wait while the engine has an entry or close in flight, and a request is refused (409) during one. The backtest runs in the learning pool behind every queued Optimizer Run. A page that fails marks the run incomplete with the bars received. With no trustworthy spread (market shut, nothing sampled) the run is refused rather than priced from the break
- [x] Per-period results (2010–17, 2018–21, 2022–26 when history reaches 2011, else thirds), bars received, first/last bar, the date, the cost model used, every trade's R
- [x] Scorecard per Arena: the Paper Account's closed trades this epoch (R = profit ÷ money risked) against the 90% bootstrap band of the matching Evidence, a verdict (within / below / above / too few trades / no evidence), and the real Account's min-lot risk % at the current stop. Paper trades are counted per Strategy, not per parameter set (trades do not record their Candidate)

### 7. API and MCP
- [x] `/api/evidence` (+ `/{id}`), `/api/evidence/match` (the editor's badge for unsaved settings), `/api/evidence/run` (202, dashboard only), `/api/sessions/{id}/evidence`, `/api/scorecard`, `/api/paper-account` (GET/PUT, + `reset`), `/api/sessions/{id}/cost-check`. The Cost Check override travels in the Session payload (`cost_overrides`: omitted = unchanged, a list = the full set; an override whose Assignment is removed is dropped; every change journaled). Changing the Paper start balance has the reset's guards and moves the Paper day start with it
- [x] `/api/research/snapshot`, `/api/research/trials` (GET/POST), `/api/research/holdout`, `/api/reviews` (+ `/{id}`), `/api/learning/challengers`. Their storage and rules were built here, because the endpoints need them (see milestone 9)
- [x] MCP: read-only `get_evidence`, `get_scorecard`, `get_research_snapshot` (≤ 500 bars; the CLI reads the full snapshot over HTTP) and `get_trials`; write tools `submit_review` and `submit_challenger` only. The whole tool set is pinned by a test. The original operator tools (start/pause/resume/stop Session, Kill Switch, run learning) are unchanged: keeping the reviewer away from them is decided with milestone 9

### 8. UI
- [x] Session editor: a Symbol once per Timeframe (e.g. GOLD H4 + GOLD H1), an Evidence badge per Assignment for the form's current settings (`/api/evidence/match`, with a "Run evidence" button), the Cost Check per Assignment for unsaved settings (`POST /api/cost-check`) with an override switch and a journaled reason (saved as the Session's full `cost_overrides` set), and the Weekend Close warning for H4/D1 with the Evidence without Weekend Close beside it
- [x] Start dialog: Cost Check result per Assignment above the Pre-flight Check; Start is disabled while one fails without an override
- [x] Strategies page: Evidence table (settings, status, trades, mean R, SQN, max drawdown, per period, history range) and "Run evidence"; statistics split real / Paper
- [x] Account page: Paper Account (equity, start balance, realised, floating, today; start balance edit and typed `RESET PAPER` reset, both disabled while Paper is active) and the graduation scorecard
- [x] Overview, History, Strategies: Paper Account and real account split (Overview adds Paper KPIs; History filters by account, real by default; the API returns them apart)
- [x] Reviews page: reports (expandable, markdown shown as text, action links only for https URLs), the trial ledger per Arena with the holdout start, and holdout results

### 9. Claude Strategy Review
- [x] `research_trials` ledger table (built in milestone 7); the per-Arena count feeds the selection penalty of reviewer Challengers, read again whenever their Guardrails are judged and never below an Optimizer pick's (`TOP_K`)
- [x] Research snapshot export with the rolling 12-month sealed holdout cut out (built in milestone 7): `research.holdout_from` — the first of the month 12 months back (3 for M1/M5), server time; a bar is research data only if it closed by then, and a trial whose data reaches it is refused
- [x] `fxcommand-research` CLI: backtest a hypothesis on the snapshot and append to the ledger (every run, pass or fail). It judges the finalist bar in one place (`research.evaluate_hypothesis`): at least 30 trades; positive and above the Champion in every research period at 2× spread with swap; SQN above max(Champion, 0) + `DEFLATE_K·sqrt(2 ln N)` for the Arena's ledger count N (this run included); every Optimizer-style neighbour positive. Champion, exit rules, window and Weekend Close come from the Arena's Session (a research-only Arena is judged against zero). New code runs as `--script` with optional `CONTEXT` timeframes aligned by `align_closed`; `holdout` needs `--confirm`
- [x] One-shot holdout evaluation, recorded in the ledger; failure is final (built in milestone 7). The ledger row is reserved before the backtest, keyed by Arena and Candidate. Pass rule: at least 5 trades opened in the holdout, positive mean R at Evidence pricing (2× spread, the Arena's rules, window and Weekend Close), and at least the Champion's mean R on the same bars
- [x] `.claude/skills/gold-review/SKILL.md`: loop, mistake categories, every-period rule, objective, authority limits, PR format with no P&L
- [x] Weekly Saturday scheduled task, plus on-demand invocation — its allowed tools exclude the operator MCP tools and its Bash is limited to the research CLI (D54). The desktop app's scheduled tasks cannot restrict tools, so the task is a Windows scheduled task (`scripts/install-review-task.ps1`, run by the operator) that starts `scripts/run-gold-review.ps1`: headless `claude -p /gold-review` with `--permission-mode dontAsk`, an allow-list (read-only MCP tools, `submit_review`, `submit_challenger`, the research CLI, pytest and the review-branch git/gh steps), the operator tools denied, only the fxcommand MCP server loaded, a budget cap, in its own git worktree. On demand: run the script, or `/gold-review` in an interactive session
- [x] CLAUDE.md reviewer rules
- [x] GOLD M5 scalping research Arena (M15 context, M1 trigger inputs; 3-month holdout); scalping hypotheses are counted in the same ledger. It needs no Session: `fxcommand-research backtest GOLD M5 --script …` with `CONTEXT = ["M15", "M1"]`, judged against zero, holdout from `holdout_from` (3 months for M1/M5)
- [x] After "Max bars in chart" was raised (2026-09-26): M1/M5/M15 re-pulled read-only from 2020 and the scalping research re-run (`scalp2.py`, `scalp3.py`). No design passed (D53)

### 10. Verify
- [ ] `uv run pytest` green
- [ ] `npm run build` clean
- [ ] `cd e2e && npm test` green, with new specs for GOLD dual Assignments, Cost Check, Paper Account, scorecard and Reviews
- [ ] `playwright-cli` pass over every page (console errors, phone width)
- [ ] `uv run pytest -m mt5 -o addopts=""` read-only, with the reopen timing and spread probe
