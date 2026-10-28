# Vibe Desk task board

The project's task list now that the Vibe Kanban board was sunset on 2026-10-28; it began as an export of that board (2026-10-24, updated 2026-10-28). `[x]` is done, `[ ]` is not; the status is in italics.

| Status | Cards |
|---|---|
| In review | 5 |
| In progress | 6 |
| To do | 2 |
| Backlog | 26 |
| Done | 48 |
| **Total** | **87** |

**Note.** Some M4 and M5 cards describe the original, fuller plan, and the simple app (CCC-83) already covers them in a simpler form. CCC-47, CCC-51 and CCC-54 are now closed as covered; CCC-52 (partly) and CCC-63 remain open:
- CCC-47 (closed), the OmniRoute adapter: `src/vibedesk/app/llm.py`;
- CCC-51 (closed), the desk page: chat, Run test, the result panel;
- CCC-52 (open), partly: Settings and the Recents list, but no separate Ideas or Ledger pages;
- CCC-54 (closed), one-click start: `Start Vibe Desk.bat` and the desktop shortcut;
- CCC-63 (open), a click-through of the app: done in the browser, not as a whole-app suite.

**Layer A team effort (2026-10-28).** Layer A is being finished by AO worker teams in waves:
- Wave 0: the Vibe Kanban board was sunset (PR #18), and the shared team contract and file ownership went into tasks/plan.md (PR #17).
- Wave 1 is in progress: the Desk team (CCC-44, CCC-46, Grounding Rule wiring), the API team (CCC-50, CCC-53, CCC-55) and the Notify team (CCC-59).
- Next come Wave 2 (the UI team, CCC-52/56; the MCP team, CCC-58/60) and Wave 3 (M7, CCC-62/63/64).
- Owner-only items: CCC-41, CCC-48, CCC-79, CCC-80, CCC-83, CCC-87 and CCC-90.

## Not done yet

### In review (5)

- [ ] **CCC-42** Roadmap · Layer A · M4 Research Desk (LLM)
- [ ] **CCC-49** Roadmap · Layer A · M5 API and web UI
- [ ] **CCC-83** M5 · Simple web app: chat an idea, AI writes the Spec, test on real XM data (under CCC-49)
- [ ] **CCC-87** M5 · Redesign the app in a claude.ai-like style (design only) (under CCC-49)
- [ ] **CCC-90** Issue · OmniRoute answers 503 resource_pressure: the shared server is out of memory (PR #14)

### In progress (6)

- [ ] **CCC-44** M4 · Desk tools: list_blocks, describe_data, draft_spec, register_hypothesis, run_trial, get_trial, request_holdout (under CCC-42)
- [ ] **CCC-46** M4 · Token budget: daily cap from OmniRoute usage, 80% warning, pause at 100% (under CCC-42)
- [ ] **CCC-50** M5 · HTTP API with owner-only actions (owner cookie, SameSite=Strict) (under CCC-49)
- [ ] **CCC-53** M5 · Scheduler: nightly data refresh and weekly Spread Floors (under CCC-49)
- [ ] **CCC-55** M5 · Holdout and Trial carry-forwards (under CCC-49)
- [ ] **CCC-59** M6 · Telegram notices and daily digest (push-only) (under CCC-57)

### To do (2)

- [ ] **CCC-41** M3 · Checkpoint C: M3 done, owner reviews (under CCC-29)
- [ ] **CCC-48** M4 · Validate: 20 sample ideas → valid Strategy Specs, and the Block Library covers the owner's last 15 ideas (under CCC-42)

### Backlog (26)

- [ ] **CCC-52** M5 · Ideas, Ledger and Settings pages (under CCC-49)
- [ ] **CCC-56** M5 · Playwright E2E on the fakes; layout at 375 px (under CCC-49)
- [ ] **CCC-57** Roadmap · Layer A · M6 MCP server and Telegram
- [ ] **CCC-58** M6 · MCP server (stdio, calls the API): read and act tools, no owner tools (under CCC-57)
- [ ] **CCC-60** M6 · Test the owner-only refusals and the RecordingNotifier (under CCC-57)
- [ ] **CCC-61** Roadmap · Layer A · M7 Whole-app test
- [ ] **CCC-62** M7 · E2E on deterministic fakes (under CCC-61)
- [ ] **CCC-63** M7 · Exploratory click-through: every page, console errors, phone width (under CCC-61)
- [ ] **CCC-64** M7 · CLAUDE.md run and test commands for the whole app (under CCC-61)
- [ ] **CCC-65** Roadmap · Layer B · Forward Proof (paper → demo → owner-gated live)
- [ ] **CCC-66** Layer B · Forward Arena: virtual paper accounts on the live feed, no orders (under CCC-65)
- [ ] **CCC-67** Layer B · Leaderboard of forward results (under CCC-65)
- [ ] **CCC-68** Layer B · Demo gate: Arena leaders graduate to the XM demo (under CCC-65)
- [ ] **CCC-69** Layer B · Re-derive the live gate for M15–H4 (under CCC-65)
- [ ] **CCC-70** Layer B · Owner-only live switch and kill switch (under CCC-65)
- [ ] **CCC-71** Layer B · Live runner re-checks time filters against the bar it fills on (under CCC-65)
- [ ] **CCC-72** Layer B · Validate: virtual fills match demo fills (2 weeks in parallel) (under CCC-65)
- [ ] **CCC-73** Layer B · Validate: the laptop runs 24/7 (week-long uptime log; sleep, updates, MT5 reconnects) (under CCC-65)
- [ ] **CCC-74** Roadmap · Layer C · Autonomy (Night Shift, Regime Desk, custom Blocks)
- [ ] **CCC-75** Layer C · Night Shift: overnight research loop, every Trial charged to the Ledger, Telegram morning brief (under CCC-74)
- [ ] **CCC-76** Layer C · Regime Desk: one typed LLM call per day switching strategies on/off, A/B-tested in the Arena (under CCC-74)
- [ ] **CCC-77** Layer C · AI-written custom Blocks, gated by tests and the owner's review (under CCC-74)
- [ ] **CCC-79** Decision · Holdout counts a trade still open at data end at its last price
- [ ] **CCC-80** Decision · atr_target receives a stop distance it ignores
- [ ] **CCC-81** Chore · Remove leftover agent worktree branches
- [ ] **CCC-82** Layer B · Basic live EA, no backtest (parked: on the board, not built) (under CCC-65)

## Roadmap, milestone by milestone

### Layer A · M1 Pure core: Blocks and engine

CCC-6 · *Done* · 8 of 8 done

- [x] **CCC-7** M1 · T1 Scaffold, guard test, Bars and SyntheticBarSource · *Done*
- [x] **CCC-8** M1 · T2 Strategy Spec schema, validation and grid expansion · *Done*
- [x] **CCC-9** M1 · T3 First slice: donchian_breakout + atr_stop + r_target + fixed_risk into trades in R · *Done*
- [x] **CCC-10** M1 · T4 Full Cost Model: short ask side, swap with Wednesday triple, M1 settling, CostSnapshot · *Done*
- [x] **CCC-11** M1 · T5 Remaining entry Blocks + look-ahead property test over every Block · *Done*
- [x] **CCC-12** M1 · T6 Filter Blocks, including exchange-local sessions with DST tests · *Done*
- [x] **CCC-13** M1 · T7 Remaining exits: atr_target, atr_trailing, time_exit, opposite_signal · *Done*
- [x] **CCC-14** M1 · T8 R metrics, daily R series, money view and the performance benchmark · *Done*

### Layer A · M2 Gauntlet and Ledger

CCC-15 · *Done* · 13 of 13 done

- [x] **CCC-16** M2 · T1 Prepared market + Block-output cache (engine speed) · *Done*
- [x] **CCC-17** M2 · T2 Synthetic planted edge + calibration · *Done*
- [x] **CCC-18** M2 · T3 Statistics: Sharpe, Deflated Sharpe, PBO · *Done*
- [x] **CCC-19** M2 · T4 Market facade + Holdout guard · *Done*
- [x] **CCC-20** M2 · T5 Ledger: families, Hypotheses, Trials, budgets, Holdout Requests · *Done*
- [x] **CCC-21** M2 · T6 Walk-forward: folds, selection, stitched blind record, stress, Holdout selection · *Done*
- [x] **CCC-22** M2 · T7 Coin-Flip Baseline with exact counts · *Done*
- [x] **CCC-23** M2 · T8 Prosecutor, Pass Bar, Verdict, Closest Miss · *Done*
- [x] **CCC-24** M2 · T9 Trial runner + Holdout run + Holdout Request gate · *Done*
- [x] **CCC-25** M2 · T10 Known-answer tests + Trial benchmark (slow) · *Done*
- [x] **CCC-26** M2 · Opposite-signal stop and reverse · *Done*
- [x] **CCC-27** M2 · Time filters judge the entry bar · *Done*
- [x] **CCC-28** M2 · rules_version: a Holdout never runs under other rules than its Trial · *Done*

### Layer A · M3 Market data from MT5 (read-only)

CCC-29 · *Done* · 11 of 12 done

- [x] **CCC-30** M3 · T0 Contract: Mt5Api, FakeMt5, dependencies, allowlist guard · *Done*
- [x] **CCC-31** M3 · T1 Server clock to UTC + Mt5BarSource · *Done*
- [x] **CCC-32** M3 · T2 Parquet cache + data-quality mask + CachedBarSource · *Done*
- [x] **CCC-33** M3 · T3 Spread Floor + swap into a CostSnapshot · *Done*
- [x] **CCC-34** M3 · Checkpoint A: T0–T3 green, owner reviews · *Done*
- [x] **CCC-35** M3 · T4 Importer and refresh (three guards against bad terminal data) · *Done*
- [x] **CCC-36** M3 · Tuning pass and independent review · *Done*
- [x] **CCC-37** M3 · T5 desk commands: smoke, import, refresh, floors, data-report · *Done*
- [x] **CCC-38** M3 · Checkpoint B: fake import + refresh read back through MarketData, owner reviews · *Done*
- [x] **CCC-39** M3 · T6 Real read-only import of all 8 symbols, data report, floors, smoke · *Done*
- [x] **CCC-40** M3 · T7 Timing and sanity run on real data (records no result) · *Done*
- [ ] **CCC-41** M3 · Checkpoint C: M3 done, owner reviews · *To do*

### Layer A · M4 Research Desk (LLM)

CCC-42 · *In review* · 3 of 6 done

- [x] **CCC-43** M4 · Desk loop with ScriptedLLM: tool calls, SSE events, ≤ 12 tool calls per message (PR #15) · *Done*
- [ ] **CCC-44** M4 · Desk tools: list_blocks, describe_data, draft_spec, register_hypothesis, run_trial, get_trial, request_holdout · *In progress*
- [x] **CCC-45** M4 · Grounding Rule validator: no digits in prose, placeholders rendered from stored data (PR #19) · *Done*
- [ ] **CCC-46** M4 · Token budget: daily cap from OmniRoute usage, 80% warning, pause at 100% · *In progress*
- [x] **CCC-47** M4 · OmniRouteLLM adapter (OpenAI-compatible, combo vibe-desk) · *Done*
- [ ] **CCC-48** M4 · Validate: 20 sample ideas → valid Strategy Specs, and the Block Library covers the owner's last 15 ideas · *To do*

### Layer A · M5 API and web UI

CCC-49 · *In review* · 3 of 10 done

- [ ] **CCC-50** M5 · HTTP API with owner-only actions (owner cookie, SameSite=Strict) · *In progress*
- [x] **CCC-51** M5 · Desk page: chat, live Trial progress, Verdict Cards · *Done*
- [ ] **CCC-52** M5 · Ideas, Ledger and Settings pages · *Backlog*
- [ ] **CCC-53** M5 · Scheduler: nightly data refresh and weekly Spread Floors · *In progress*
- [x] **CCC-54** M5 · One-click start: double-click sets up and opens the app · *Done*
- [ ] **CCC-55** M5 · Holdout and Trial carry-forwards · *In progress*
- [ ] **CCC-56** M5 · Playwright E2E on the fakes; layout at 375 px · *Backlog*
- [ ] **CCC-83** M5 · Simple web app: chat an idea, AI writes the Spec, test on real XM data · *In review*
- [ ] **CCC-87** M5 · Redesign the app in a claude.ai-like style (design only) · *In review*
- [x] **CCC-88** Bug · Old chats didn't load: save chats and open them again · *Done*

### Layer A · M6 MCP server and Telegram

CCC-57 · *Backlog* · 0 of 3 done

- [ ] **CCC-58** M6 · MCP server (stdio, calls the API): read and act tools, no owner tools · *Backlog*
- [ ] **CCC-59** M6 · Telegram notices and daily digest (push-only) · *In progress*
- [ ] **CCC-60** M6 · Test the owner-only refusals and the RecordingNotifier · *Backlog*

### Layer A · M7 Whole-app test

CCC-61 · *Backlog* · 0 of 3 done

- [ ] **CCC-62** M7 · E2E on deterministic fakes · *Backlog*
- [ ] **CCC-63** M7 · Exploratory click-through: every page, console errors, phone width · *Backlog*
- [ ] **CCC-64** M7 · CLAUDE.md run and test commands for the whole app · *Backlog*

### Layer B · Forward Proof (paper → demo → owner-gated live)

CCC-65 · *Backlog* · 0 of 9 done

- [ ] **CCC-66** Layer B · Forward Arena: virtual paper accounts on the live feed, no orders · *Backlog*
- [ ] **CCC-67** Layer B · Leaderboard of forward results · *Backlog*
- [ ] **CCC-68** Layer B · Demo gate: Arena leaders graduate to the XM demo · *Backlog*
- [ ] **CCC-69** Layer B · Re-derive the live gate for M15–H4 · *Backlog*
- [ ] **CCC-70** Layer B · Owner-only live switch and kill switch · *Backlog*
- [ ] **CCC-71** Layer B · Live runner re-checks time filters against the bar it fills on · *Backlog*
- [ ] **CCC-72** Layer B · Validate: virtual fills match demo fills (2 weeks in parallel) · *Backlog*
- [ ] **CCC-73** Layer B · Validate: the laptop runs 24/7 (week-long uptime log; sleep, updates, MT5 reconnects) · *Backlog*
- [ ] **CCC-82** Layer B · Basic live EA, no backtest (parked: on the board, not built) · *Backlog*

### Layer C · Autonomy (Night Shift, Regime Desk, custom Blocks)

CCC-74 · *Backlog* · 0 of 3 done

- [ ] **CCC-75** Layer C · Night Shift: overnight research loop, every Trial charged to the Ledger, Telegram morning brief · *Backlog*
- [ ] **CCC-76** Layer C · Regime Desk: one typed LLM call per day switching strategies on/off, A/B-tested in the Arena · *Backlog*
- [ ] **CCC-77** Layer C · AI-written custom Blocks, gated by tests and the owner's review · *Backlog*

## Issues, decisions and chores

### Issues (3)

- [x] **CCC-78** Issue · MT5 terminal looked stale: last GOLD tick ~19 h old (2026-10-23) · *Done*
- [x] **CCC-85** Issue · XM clock checks fail most years: likely European daylight-saving switch (PR #13) · *Done*
- [ ] **CCC-90** Issue · OmniRoute answers 503 resource_pressure: the shared server is out of memory (PR #14) · *In review*

### Decisions (3)

- [ ] **CCC-79** Decision · Holdout counts a trade still open at data end at its last price · *Backlog*
- [ ] **CCC-80** Decision · atr_target receives a stop distance it ignores · *Backlog*
- [x] **CCC-84** Decision · Rigor rules dropped: simple app, no orders stays (owner, 2026-10-23) · *Done*

### Chores (5)

- [ ] **CCC-81** Chore · Remove leftover agent worktree branches · *Backlog*
- [x] **CCC-86** Chore · Owner: add the OmniRoute key in Settings and try the AI chat · *Done*
- [x] **CCC-89** Chore · AGENTS.md handoff for any agent · *Done*
- [x] **CCC-91** Chore · Pushed to GitHub: marknatdev/fxcommand main · *Done*
- [x] **CCC-92** Chore · README for the GitHub page · *Done*
