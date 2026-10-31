# Remove unused stop-distance input parameter from atr_target exit Block

In `src/vibedesk/blocks/exits.py`, the `atr_target` exit Block definition accepted a `stop_distance` argument alongside `atr` and `multiplier`.

Inspection revealed that `atr_target` computes profit targets strictly as:
$$\text{target\_price} = \text{entry\_price} \pm (\text{multiplier} \times \text{atr})$$
The `stop_distance` parameter was never referenced in the target price calculation.

We resolve CCC-80 by removing `stop_distance` from the Block's parameter specification and signature.

Because `stop_distance` was completely ignored by the execution engine, this refactoring does not alter simulated trade sequences, execution prices, or backtest results.

## Considered Options

- **Retain the unused parameter:** preserves existing signatures, but clutters the interface and confuses Strategy Spec generation.
- **Remove the unused parameter (chosen):** cleanly aligns the code signature with actual execution logic without affecting backtest calculations or requiring a `RULES_VERSION` bump.
