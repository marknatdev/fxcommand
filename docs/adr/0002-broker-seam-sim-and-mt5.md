# One Broker seam with a simulated and an MT5 adapter

All market access (quotes, bars, positions, orders) goes through a single small `Broker` interface with two adapters: `Mt5Broker` (the real terminal) and `SimBroker` (a seeded, in-memory random-walk market that fills orders and hits stop-losses itself). The app runs in `BROKER=sim` by default. All automated tests that place orders — unit, integration and Playwright end-to-end — run on `SimBroker`; the MT5 adapter is only exercised by a read-only smoke test. This lets us test the whole trading loop deterministically without ever risking money, at the cost of maintaining a simulator whose behaviour must stay close to the real terminal (contract sizes, stop levels, volume steps).

`SimBroker` additionally exposes test controls (advance bars, price shock) through `/api/sim/*`; these endpoints refuse to work unless the app is in sim mode.
