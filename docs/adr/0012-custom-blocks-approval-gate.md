# Custom Blocks require look-ahead verification and owner UI approval before use

Layer C introduces AI-authored custom Blocks to expand beyond the fixed Block Library.

To protect the integrity of the Verdict Engine:
1. Custom block Python code lives strictly in `src/vibedesk/blocks/custom/{name}.py`. Core code in `src/vibedesk/blocks/` cannot be modified by the AI.
2. The block must pass automated verification: syntax checks, parameter bounds declaration, and the look-ahead property test (`test_custom_block_lookahead`).
3. Even after passing automated tests, a custom block is marked unapproved (`approved=False`).
4. An unapproved block cannot be referenced in any Strategy Spec or run in any Trial.
5. The owner must review the code in the web UI and explicitly click "Approve", which records the approval and code hash in the Ledger.

## Considered Options

- **Allow AI to edit core Blocks directly:** simplest for prompt engineering, but risks corrupting existing verified blocks and breaking historical backtest reproducibility.
- **Auto-approve blocks that pass property tests:** reduces friction, but risks subtle overfitting, excessive parameter grids, or complex indicator logic that escapes synthetic property tests.
- **Isolated directory with property tests and owner approval gate (chosen):** guarantees core engine stability, verifies absence of look-ahead bias, and keeps final architectural control with the owner.
