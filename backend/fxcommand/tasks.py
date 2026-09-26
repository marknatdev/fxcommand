"""Stopping background asyncio tasks reliably."""

from __future__ import annotations

import asyncio


async def cancel_and_wait(task: asyncio.Task | None, poll: float = 0.5) -> None:
    """Cancel ``task`` and wait until it has really ended.

    Python 3.10's ``asyncio.wait_for`` (used by ``BrokerThread.run``) can swallow a cancel that lands
    just as the awaited call returns; a loop task then carries on as if nothing happened and a plain
    ``cancel(); await task`` waits forever. So cancel again until the task is done. Its exception,
    if any, is retrieved and dropped: a stopping task must never stop the shutdown.
    """
    if task is None:
        return
    while not task.done():
        task.cancel()
        await asyncio.wait({task}, timeout=poll)
    if not task.cancelled():
        task.exception()
