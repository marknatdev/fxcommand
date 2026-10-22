# Strategies are compositions of Blocks, not AI-written code

The Research Desk expresses every strategy as a Strategy Spec: typed JSON composed from a fixed Block Library. It does not write free-form Python. This makes every strategy validatable, deterministic, comparable to other strategies and cheap to backtest on one vectorised engine. It also removes any need for a code sandbox. We accept that some ideas will not fit the Blocks. AI-written custom Blocks come later (Layer C), and only after tests and the owner's review.

## Considered Options

- **AI writes Python strategies (Vibe-Trading style)**: the most flexible option, but it can't be checked, it is easy to overfit, and it needs sandboxing.
- **LLM decides each trade (nofx Autopilot)**: it pays for an API call every cycle and the owner's earlier evidence showed no edge. The owner rejected it.
