# Demo-Ready qualification badge with audited owner override

A strategy in the Forward Arena qualifies for the Demo-Ready badge when it satisfies objective stability criteria:
- Active duration $\ge 4$ weeks;
- Forward trade count $\ge 30$;
- Forward Profit Factor $\ge 1.10$;
- Maximum Drawdown $< 6.0\%$;
- No performance decay relative to its Gauntlet training distribution.

Qualification renders a badge in the UI and pushes a Telegram notice.

The owner retains full authority to bypass this gate via "mark ready anyway". Any manual override requires entering a reason and is immutably recorded in the Ledger table `arena_overrides`.

## Considered Options

- **Strict algorithmic gate without override:** prevents premature graduation, but removes the owner's discretion to fast-track setups based on external context or market conditions.
- **Unlogged UI flag toggle:** allows discretionary flexibility, but breaks auditability and obscures whether a strategy earned its status empirically.
- **Algorithmic badge with audited Ledger override (chosen):** maintains an uncompromised automated benchmark while allowing owner overrides that are permanently logged with stated reasons.
