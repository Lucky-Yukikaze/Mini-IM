from __future__ import annotations

import asyncio
from dataclasses import dataclass

from storage.sqlite.db import MiniImSqliteDb


@dataclass
class WriteTask:
    sql: str
    params: tuple[object, ...]


class SqliteWriteQueue:
    def __init__(self, db: MiniImSqliteDb) -> None:
        self.m_db = db
        self.m_queue: asyncio.Queue[WriteTask] = asyncio.Queue()
        self.m_worker_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.m_worker_task is None:
            self.m_worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        if self.m_worker_task is None:
            return
        self.m_worker_task.cancel()
        try:
            await self.m_worker_task
        except asyncio.CancelledError:
            pass
        self.m_worker_task = None

    async def submit(self, sql: str, params: tuple[object, ...] = ()) -> None:
        await self.m_queue.put(WriteTask(sql=sql, params=params))

    async def _worker_loop(self) -> None:
        while True:
            task = await self.m_queue.get()
            self.m_db.execute_write(task.sql, task.params)
            self.m_queue.task_done()
