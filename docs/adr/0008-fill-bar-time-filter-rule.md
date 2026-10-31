# Time filters evaluate the fill bar t+1 in backtest and Arena

A trading signal generated at the close of bar $t$ fills at the open of bar $t+1$. Previously, time and session filters only checked whether bar $t$ satisfied the filter. This allowed entries to fill into forbidden sessions or across weekend closures if bar $t$ fell inside the window but bar $t+1$ fell outside.

We now require that time filters (e.g. `session_window`, `day_of_week`) be satisfied on both bar $t$ and bar $t+1$. If bar $t+1$ fails the filter, the entry is skipped and logged.

Because this change alters simulated fills and trade counts on historical bars, it requires bumping `RULES_VERSION` from 3 to 4. Previous Trials in the Ledger remain pinned under version 3 and cannot be combined with version 4 Holdouts.

## Considered Options

- **Check bar t only (previous behavior):** simpler, but allowed fills to execute into market closures or illiquid weekend opens with extreme spreads.
- **Cancel orders dynamically at run time only:** creates divergence between the Gauntlet backtest and the Forward Arena.
- **Evaluate bar t+1 in both backtest and Arena (chosen):** guarantees exact parity between backtest simulation and forward paper execution.
