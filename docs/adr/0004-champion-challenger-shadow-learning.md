# Self-improvement by Champion/Challenger Shadow Trading with guarded Promotion

Strategies improve by competition, not by re-fitting live parameters. Each Arena (Symbol + Timeframe) keeps up to three Challengers produced by a Walk-forward Optimizer; the Champion and the Challengers all Shadow Trade the same real closed bars under the live rules, so every trading session produces a like-for-like comparison. A Challenger replaces the Champion only through a Promotion that passes Guardrails (≥30 Shadow Trades, beats the Champion on the same bars, better deflated out-of-sample score, robust to neighbouring parameters, drawdown not >20% worse) and only when the Assignment is flat; the last 20 live trades after a Promotion are monitored and a failing Promotion is rolled back automatically. Auto-promotion is allowed on demo/simulated accounts only — on a live account a human always promotes. Automatic rollback is allowed everywhere, including live: it only restores a Champion the operator (or the Guardrails on demo) already approved.

We rejected continuously re-optimising live parameters (curve-fits to noise and changes behaviour mid-trade with no evidence it helps) and ML/LLM strategy generation for v1 (too little data per Arena to trust).

## Consequences

- Everything is measured in R, so results compare across symbols and account sizes, and a tiny demo account still produces learning data from Shadow Trades even when the Risk Gate rejects every live order for size.
- A running Session's Champion can change without a restart; the Journal records every Champion Version.
