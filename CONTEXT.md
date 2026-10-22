# Vibe Desk

Vibe Desk is a research desk for one owner who trades GOLD and FX on MT5. The owner types trading ideas in chat. An AI turns each idea into a testable strategy and gives an honest verdict. Only strategies that survive the verdict go on to trade.

## Research

**Idea**:
The owner's free-text description of a trading thought, as typed in chat.
_Avoid_: prompt, request

**Strategy Spec**:
A strategy written as a composition of Blocks, with declared parameter ranges. It is the only form in which a strategy exists.
_Avoid_: strategy code, bot, EA

**Block**:
A named, typed building piece of a Strategy Spec. Each Block is an entry, a filter, an exit or a sizing rule.
_Avoid_: indicator, component, module

**Block Library**:
The fixed set of Blocks the AI may compose from.
_Avoid_: toolbox, catalog

**Hypothesis**:
A testable claim that is frozen before testing. It consists of a Strategy Spec, a symbol, a timeframe and the Pass Bar that applies.
_Avoid_: experiment, test, setup

**Idea Family**:
All Hypotheses derived from the same Idea. They share one Holdout spend.
_Avoid_: project, group

**Trial**:
One walk-forward run of one Strategy Spec, covering its whole parameter grid. Every run is a Trial, including exploratory ones.
_Avoid_: run, backtest (as a count)

**Ledger**:
The permanent, append-only record of every Hypothesis and Trial. The Pass Bar's deflation is computed from it.
_Avoid_: log, history, journal

## Verdicts

**Gauntlet**:
The fixed sequence of checks a Hypothesis must survive to earn a passing Verdict.
_Avoid_: pipeline, validation suite

**Research Window**:
The data the AI may see and test on freely: 2014-01-01 to 2024-12-31.
_Avoid_: in-sample, training data

**Holdout**:
The data from 2025-01-01 onward. It is hidden from the AI and spent at most once per Idea Family.
_Avoid_: test set, out-of-sample (when used loosely)

**Cost Model**:
The rules that charge each simulated trade its spread, slippage and swap.
_Avoid_: fees, commission model

**Spread Floor**:
The per-symbol minimum spread in the Cost Model, measured from recent real ticks. Recorded bar spreads understate true cost, which is why the floor exists.

**Coin-Flip Baseline**:
Random entries with the same trade count, direction mix, exits and sizing as the Hypothesis. A Hypothesis must clearly beat it.
_Avoid_: random benchmark

**Prosecutor**:
The part of the Gauntlet that runs fixed checks for luck (profit concentration, yearly consistency, parameter sensitivity, side and regime splits) and explains the results in plain language.
_Avoid_: critic, judge, red team

**Pass Bar**:
The thresholds a Hypothesis must meet in the Gauntlet. It gets stricter as the Ledger's Trial count grows.
_Avoid_: criteria, targets

**Walk-Forward Fold**:
One year of blind trading. The parameters are chosen on the three years before it. The Research Window yields eight folds (2017–2024).
_Avoid_: split, window

**Trial Budget**:
The number of Trials the Research Desk may run for one Idea Family before it must stop and report. Only the owner can extend it.

**Holdout Request**:
A request, made by the Research Desk or by the owner, to spend an Idea Family's Holdout. Only the owner can approve it.

**Verdict**:
The Gauntlet's outcome for a Hypothesis. A Verdict is one of the following:
- **REJECT**: the default.
- **CANDIDATE**: passed the Research Window checks; the Holdout is not spent yet.
- **PASS**: also passed the Holdout.
- **HOLDOUT FAIL**: failed the Holdout; this is final for the Idea Family.

**Verdict Card**:
The display of a Verdict. Every number on it comes from Trial results.
_Avoid_: report, summary

**R**:
The money a single trade risks: the distance from entry to stop. Results are measured in R so that position size does not matter.

**Closest Miss**:
The check a rejected Hypothesis came nearest to passing, shown with by how much it failed.

## Agents

**Research Desk**:
The AI agent that turns Ideas into Hypotheses, runs Trials through tools and explains Verdicts.
_Avoid_: bot, assistant, copilot

**Grounding Rule**:
The Research Desk may not write numbers in its own words. Every figure it shows comes from a Trial result.
