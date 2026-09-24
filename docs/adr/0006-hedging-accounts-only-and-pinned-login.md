# Hedging accounts only; Sessions pinned to the login they started on

Ownership by Magic Number (ADR 0003) assumes every order opens its own position. On a netting account the terminal keeps one position per Symbol, merging orders from every Session and from manual trading, so "which positions are mine" has no answer. The engine therefore refuses to start any Session unless the Account's margin mode is hedging, and the Pre-flight Check reports it.

The terminal can be switched to another Account while the application runs. A Session keeps the login it started on; if the connected login changes, every active Session becomes Interrupted and an Alert fires. Without this, switching to a previously live-enabled Account would resume trading on it silently.

## Considered Options

- Supporting netting by tracking ownership in our database and trading position deltas — large, error-prone, and would break re-adoption from the terminal as source of truth. Rejected.
- Following the login switch (re-pinning) — silent live trading on another Account. Rejected.
