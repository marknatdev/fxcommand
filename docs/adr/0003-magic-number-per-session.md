# Position ownership by one Magic Number per Session, one Symbol per Session

Each Session gets a unique, never-reused MT5 magic number, stamped on every order it sends. Position ownership is `(magic, symbol)`, which is unambiguous because a Symbol may appear only once per Session and in at most one running Session at a time. This keeps the engine away from manual or foreign trades, lets a restarted Session re-adopt its positions straight from the terminal (the terminal, not our database, is the source of truth for open positions), and makes the Kill Switch "close everything with one of our magic numbers".

## Considered Options

- Magic number per Assignment — would allow the same Symbol on two timeframes, but multiplies magic numbers and complicates Kill Switch and re-adoption. Rejected for v1; it is the upgrade path if multi-timeframe-per-Symbol is needed.
- Tracking ownership by ticket in our database only — breaks when the database and terminal diverge (crash between order send and commit).
