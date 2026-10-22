# Vibe Desk

Idea one-pager (idea-refine Phase 3), locked with the owner on 2026-10-21. Intent: [docs/intent/vibe-desk.md](../intent/vibe-desk.md).

## Problem Statement

How might we turn the owner's trading ideas into verdicts they can trust within minutes, and let only the survivors trade themselves, without the system fooling them the way both earlier builds did?

## Recommended Direction

Vibe Desk is one product built in three layers, in a fixed order. Each layer ships and proves itself before the next one starts.

**Layer A: Verdict Engine.** This is the MVP. The owner describes an idea in chat. The Research Desk agent (on OmniRoute, through one LLM seam) translates the idea into a typed **Strategy Spec**: a JSON composition of blocks from a fixed library of entries, filters, exits and sizing. The AI never writes free-form code in Layer A.

The spec then runs through the **Gauntlet**:
- an in-sample and walk-forward backtest on one vectorised engine with a realistic spread and cost model
- a blind holdout
- a 2× spread stress test
- the coin-flip baseline
- Deflated Sharpe and PBO
- a Prosecutor pass that argues the result is luck

Every test is pre-registered in the **Hypothesis Ledger**. The Ledger counts every trial, raises the pass bar as the count grows, and allows the holdout to be spent only once per idea family.

The result is a **Verdict Card**, with REJECT as the default. When an idea fails, the card shows the closest miss and the reason. The grounding rule: every number on the card comes from tool output and never from the model's text.

**Layer B: Forward Proof.** Strategies that pass run in the **Forward Arena** on the live MT5 feed. Each one has its own virtual paper account, and none of them sends orders. A leaderboard ranks their forward results. Arena leaders graduate to the XM demo, and from there to live through a re-derived gate. Only the owner can switch live on.

**Layer C: Autonomy.** The **Night Shift** runs an overnight research loop that mutates ideas through the Gauntlet. Every trial is charged to the Ledger, and a Telegram morning brief reports the results. The **Regime Desk** makes one typed LLM call per day, which switches strategies on or off. It never blocks exits and changes nothing when it fails. The Regime Desk is itself tested as an A/B inside the Arena. **AI-written custom blocks** also arrive in this layer: Python blocks that enter the library only after tests and the owner's review, and that pass the same Gauntlet.

### Patterns borrowed from the reference repos

| Repo | Patterns |
|---|---|
| Vibe-Trading (MIT) | Chat agent with typed tools; a grounding gate on figures; paper accounts that only move forward; a kill switch that persists across restarts; an MCP server |
| QuantDinger | Deterministic checks before any AI call; AI that never blocks exits and changes nothing when it fails; typed AI decisions; walk-forward plus a blind holdout, PBO and Deflated Sharpe; signal-only virtual accounts |
| nofx (AGPL, patterns only) | A typed decision schema with confidence; a decision log with the reasoning; protective stops placed at the broker; reconcile instead of retry; a leaderboard |

## Key Assumptions to Validate

- [ ] **The XM MT5 history is deep and honest enough.** This means bars plus spread for GOLD and FX majors on M15–H4. Test: a read-only probe that counts the years of bars and checks the spread fields per symbol and timeframe.
- [ ] **OmniRoute's model turns ideas into valid Strategy Specs.** Test: 20 sample ideas through the real model. Measure the share that produce a valid spec on the first try, and the share that match the owner's intent.
- [ ] **The block library covers the owner's ideas.** Test: write the owner's last 15 ideas as specs by hand before building. Count how many fit.
- [ ] **Something can pass the Gauntlet at all.** Test: run known textbook strategies through it. If nothing ever passes, the closest-miss view has to carry the product.
- [ ] **The live gate fits the M15–H4 horizon.** The "4 weeks, 100 trades" gate came from scalping. Re-derive it from the expected trades per week and a statistical bound before Layer B.
- [ ] **Virtual fills match demo fills.** Test: run one strategy in the Arena and on the demo in parallel for 2 weeks and compare the fills.
- [ ] **The laptop runs 24/7.** That means sleep, Windows updates and MT5 reconnects are handled. Test: a week-long uptime log before Layer B goes live.

## MVP Scope (Layer A only)

**In:**
- Local web app with a chat panel, Verdict Cards and the Ledger view
- The Research Desk agent on OmniRoute (real adapter plus a fake adapter for tests)
- A block library v1 (a small set of entries, filters, exits and sizing blocks)
- Strategy Spec validation
- A vectorised backtest engine with a spread and cost model
- The Gauntlet (walk-forward, holdout, spread stress, coin-flip baseline, Deflated Sharpe, PBO, Prosecutor)
- The Hypothesis Ledger
- The grounding rule
- Read-only MT5 history import, with a fake data adapter for tests
- An MCP server exposing research, backtest and verdict tools
- Telegram for verdict notifications only

**Out of the MVP:** Everything in Layers B and C, including any live-feed runtime, virtual accounts, demo orders and the kill switch UI.

## Not Doing (and Why)

- **An LLM that decides every trade (nofx Autopilot)**: it costs API money on every cycle and has no proven edge. The owner chose research-and-run.
- **Crypto exchanges and stocks**: the owner trades GOLD and FX on XM MT5. Each extra broker is another seam to build and test.
- **AI promoting to live**: only the owner can switch live on, by the owner's rule.
- **Free-form AI strategy code in the MVP**: it can't be checked and it's easy to overfit with. Custom blocks come in Layer C, gated.
- **Reusing code from `D:\fxcommand`**: the owner wants a fresh codebase. Lessons carry over, code doesn't.
- **Copying nofx code**: its AGPL-3.0 licence would spread to this project. Patterns only.
- **Multiple users, a login, a server or Linux deployment, mobile UI**: one owner on one laptop.
- **AI increasing position size**: the AI can only skip trades or reduce risk, never add risk.
- **Scalping horizons (seconds to minutes)**: earlier evidence showed the GOLD spread destroys tight-stop strategies. Start at M15–H4.

## Open Questions

- What is the exact block library v1? It should come from the owner's real ideas, not from a textbook.
- Which data range is the holdout? It is fixed once and never moved.
- What Gauntlet thresholds count as PASS (PF, trade count, Deflated Sharpe, PBO)? How fast does the bar rise with the trial count?
- What is the live gate for M15–H4: trades, time, or a statistical bound?
- Where does the Regime Desk get its economic calendar and news (Layer C)?
- Which OmniRoute combo and model does the Research Desk use, and what is the monthly cost ceiling?
- Does the Ledger hold the true trial count across the MCP server and the web app (one store)?
