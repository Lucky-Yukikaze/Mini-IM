from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
import inspect
from typing import TypeVar

T = TypeVar("T")


class WriteQueueFull(RuntimeError):
    """The operation was not accepted; its caller must preserve retry semantics."""


class SqliteWriteQueue:
    """Serialize complete synchronous operations on the owning event loop.

    Each operation owns its business transaction and may publish only after
    committing. This queue does not wrap callbacks in another transaction.
    Accepted work survives cancellation of its caller; stop drains that work.
    """

    def __init__(self, max_operations: int = 1024, max_payload_bytes: int = 33554432) -> None:
        if max_operations <= 0 or max_payload_bytes <= 0:
            raise ValueError("write queue limits must be positive")
        self.max_operations = max_operations
        self.max_payload_bytes = max_payload_bytes
        self.pending_payload_bytes = 0
        self.m_queue = deque()
        self.m_worker_task: asyncio.Task[None] | None = None
        self.m_accepting = True

    def enqueue(self, operation: Callable[[], T], *, payload_bytes: int = 0) -> asyncio.Future[T]:
        if not self.m_accepting:
            raise RuntimeError("write queue is stopping")
        if not callable(operation) or inspect.iscoroutinefunction(operation):
            raise TypeError("write queue requires a synchronous operation")
        if payload_bytes < 0:
            raise ValueError("payload size must not be negative")
        if (len(self.m_queue) >= self.max_operations
                or payload_bytes > self.max_payload_bytes - self.pending_payload_bytes):
            raise WriteQueueFull("write queue capacity exceeded")
        result = asyncio.get_running_loop().create_future()
        self.m_queue.append((operation, result, payload_bytes))
        self.pending_payload_bytes += payload_bytes
        if self.m_worker_task is None or self.m_worker_task.done():
            self.m_worker_task = asyncio.create_task(self._worker_loop())
        return result

    async def submit(self, operation: Callable[[], T]) -> T:
        return await asyncio.shield(self.enqueue(operation))

    async def stop(self) -> None:
        self.m_accepting = False
        if self.m_worker_task is not None:
            await asyncio.shield(self.m_worker_task)

    async def _worker_loop(self) -> None:
        while self.m_queue:
            operation, result, payload_bytes = self.m_queue.popleft()
            self.pending_payload_bytes -= payload_bytes
            try:
                value = operation()
                if inspect.isawaitable(value):
                    if inspect.iscoroutine(value):
                        value.close()
                    raise TypeError("write operation must complete without awaiting")
            except (Exception, asyncio.CancelledError) as error:
                if not result.done():
                    result.set_exception(error)
            else:
                if not result.done():
                    result.set_result(value)
            # Keep QUIC timers and other callers responsive between operations.
            await asyncio.sleep(0)
