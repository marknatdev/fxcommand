# Python + FastAPI backend, because MT5 only speaks Python

The official `MetaTrader5` package is the only supported programmatic route into the MT5 terminal, and it is Windows-only, CPython-only, blocking and process-global. So the engine and the HTTP/WebSocket API live together in a single Python 3.10 process (FastAPI + asyncio), with every terminal call funnelled through one dedicated thread. The dashboard is a separate React/Vite app that talks only to that API.

## Consequences

- Run with exactly one uvicorn worker and no auto-reload: a second process would start a second engine and fight over the terminal connection.
- The app only runs on the Windows machine that hosts the terminal.
