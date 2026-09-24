# Paper execution as a Broker router; uncertain orders are never retried

**Paper Execution Mode.** Paper Sessions reuse the whole engine — Strategy, Risk Gate, position management, Journal, learning — on the Broker's real prices. Instead of a second engine, one adapter (`PaperRouter`) wraps the real Broker: reads pass through; order methods for a Paper Session's Magic Number (and for Paper tickets, which are negative) go to a local Paper book persisted in SQLite; everything else goes to the real Broker. The Paper book only ever holds a read-only view of the real Broker whose order methods raise, so a routing bug cannot send an order. Paper positions have no server-side stops, so a Paper Session always closes them when it stops, and on restart they are settled from the M1 bars missed while the application was down (stop-loss first, gaps fill at the open).

**Order Outcomes.** MT5 answers an order with a retcode. We classify it:

- *filled* (`DONE`, `DONE_PARTIAL`, `PLACED`): the ticket, price and volume are read back from the resulting position, because some servers report price 0 on market execution;
- *not executed* (requote, price changed/off, market closed, no money, invalid …): nothing exists; the engine may retry once, re-running the Risk Gate against a fresh tick in the same broker call;
- *uncertain* (timeout, connection, no reply): a position may exist. Entries are never retried; the engine re-reads positions and adopts any Orphan.

Closes are idempotent (closing a position that is gone is harmless) and are retried until the terminal confirms the position is gone.

## Considered Options

- A separate paper engine — duplicates the core and drifts from live behaviour. Rejected.
- Retrying uncertain entries — risks two positions for one Signal. Rejected.
