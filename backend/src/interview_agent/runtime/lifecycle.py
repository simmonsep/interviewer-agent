"""Track owned coroutines so cancellation-resistant providers cannot silently escape."""

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


class OwnedTasks:
    def __init__(self):
        self.tasks: set[asyncio.Task] = set()

    def track(self, coroutine: Awaitable[T]) -> asyncio.Task[T]:
        task = asyncio.ensure_future(coroutine)
        self.tasks.add(task)

        def completed(done):
            self.tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(completed)
        return task

    async def call(self, coroutine: Awaitable[T]) -> T:
        task = self.track(coroutine)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            raise

    async def settle(self, grace_s: float) -> bool:
        pending = {t for t in self.tasks if not t.done()}
        if not pending:
            return True
        for task in pending:
            task.cancel()
        _, remaining = await asyncio.wait(pending, timeout=grace_s)
        return not remaining
