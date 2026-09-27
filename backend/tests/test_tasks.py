"""cancel_and_wait: a task that swallows a cancel (as Python 3.10's wait_for can) is still stopped."""

import asyncio

from fxcommand.tasks import cancel_and_wait


async def test_a_task_that_swallows_one_cancel_is_still_stopped():
    swallowed = 0

    async def loop():
        nonlocal swallowed
        while True:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                if swallowed:
                    raise
                swallowed += 1  # like wait_for returning the result instead of raising

    task = asyncio.create_task(loop())
    await asyncio.sleep(0)
    await asyncio.wait_for(cancel_and_wait(task, poll=0.05), 5)
    assert task.cancelled() and swallowed == 1


async def test_failed_or_missing_tasks_do_not_raise():
    async def boom():
        raise RuntimeError("x")

    task = asyncio.create_task(boom())
    await asyncio.sleep(0)
    await cancel_and_wait(task)
    await cancel_and_wait(None)
