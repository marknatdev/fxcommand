# The app process is the only writer; the MCP server is a client of its API

The Ledger must hold the true Trial count no matter whether the owner works from the web app or from Claude Code. The MCP server therefore never opens the database. It calls the app's HTTP API, and the app process is the single writer of the SQLite store (Ledger, Strategy Specs, results) and of the Parquet bar cache. Letting both processes open the same SQLite file would be less code, but it would allow two writers, split trial counting and duplicate business rules.
