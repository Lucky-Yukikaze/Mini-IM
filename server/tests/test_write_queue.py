"""Whole-operation serialization, transactional failure and shutdown behavior."""
import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from storage.sqlite.db import MiniImSqliteDb
from storage.sqlite.write_queue import SqliteWriteQueue, WriteQueueFull


class WriteQueueTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "test.db"
        self.db = MiniImSqliteDb(self.path)
        self.db.execute_write("CREATE TABLE effects (id INTEGER PRIMARY KEY, value TEXT)")
        self.queue = SqliteWriteQueue()

    async def asyncTearDown(self):
        await self.queue.stop()
        self.db.close()
        self.temporary.cleanup()

    def values(self):
        with closing(sqlite3.connect(self.path)) as observer:
            return observer.execute("SELECT value FROM effects ORDER BY id").fetchall()

    def write_pair(self, value, fail=False):
        with self.db.transaction() as connection:
            connection.execute("INSERT INTO effects(value) VALUES (?)", (value + "-first",))
            self.assertNotIn((value + "-first",), self.values())
            if fail:
                raise ValueError("injected transaction failure")
            connection.execute("INSERT INTO effects(value) VALUES (?)", (value + "-second",))
        return self.values()

    async def test_complete_transactions_run_in_order_and_return_after_commit(self):
        first = self.queue.enqueue(lambda: self.write_pair("one"))
        second = self.queue.enqueue(lambda: self.write_pair("two"))
        self.assertEqual([], self.values())
        self.assertEqual([("one-first",), ("one-second",)], await first)
        self.assertEqual([("one-first",), ("one-second",), ("two-first",), ("two-second",)], await second)

    async def test_failure_rolls_back_and_later_operation_still_runs(self):
        failed = self.queue.enqueue(lambda: self.write_pair("failed", True))
        following = self.queue.enqueue(lambda: self.write_pair("following"))
        with self.assertRaisesRegex(ValueError, "injected"):
            await failed
        self.assertEqual([("following-first",), ("following-second",)], await following)

    async def test_cancelled_result_does_not_discard_accepted_operation(self):
        result = self.queue.enqueue(lambda: self.write_pair("accepted"))
        result.cancel()
        await self.queue.stop()
        self.assertEqual([("accepted-first",), ("accepted-second",)], self.values())

    async def test_cancelled_submit_caller_preserves_accepted_transaction(self):
        caller = asyncio.create_task(self.queue.submit(lambda: self.write_pair("accepted")))
        await asyncio.sleep(0)
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        await self.queue.stop()
        self.assertEqual([("accepted-first",), ("accepted-second",)], self.values())

    async def test_stop_drains_work_and_rejects_new_submissions(self):
        results = [self.queue.enqueue(lambda n=n: self.write_pair(str(n))) for n in range(3)]
        await self.queue.stop()
        self.assertEqual(6, len(self.values()))
        self.assertTrue(all(result.done() and result.exception() is None for result in results))
        with self.assertRaisesRegex(RuntimeError, "stopping"):
            self.queue.enqueue(lambda: self.write_pair("late"))
        await self.queue.stop()

    async def test_worker_remains_usable_after_idle(self):
        await self.queue.submit(lambda: self.write_pair("first"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(4, len(await self.queue.submit(lambda: self.write_pair("second"))))

    async def test_capacity_rejection_has_no_effect_and_drains_accepted_work(self):
        queue = SqliteWriteQueue(max_operations=2, max_payload_bytes=8)
        first = queue.enqueue(lambda: self.write_pair("first"), payload_bytes=3)
        second = queue.enqueue(lambda: self.write_pair("second"), payload_bytes=5)
        second.cancel()
        with self.assertRaises(WriteQueueFull):
            queue.enqueue(lambda: self.write_pair("rejected"))
        self.assertEqual(8, queue.pending_payload_bytes)
        await queue.stop()
        await first
        self.assertEqual(0, queue.pending_payload_bytes)
        self.assertEqual(4, len(self.values()))

    async def test_byte_capacity_released_after_failed_operation(self):
        queue = SqliteWriteQueue(max_operations=8, max_payload_bytes=7)
        failed = queue.enqueue(lambda: self.write_pair("rollback", True), payload_bytes=7)
        with self.assertRaises(WriteQueueFull):
            queue.enqueue(lambda: self.write_pair("rejected"), payload_bytes=1)
        with self.assertRaises(ValueError):
            await failed
        self.assertEqual(0, queue.pending_payload_bytes)
        self.assertEqual([], self.values())
        await queue.enqueue(lambda: self.write_pair("retry"), payload_bytes=7)
        with self.assertRaises(WriteQueueFull):
            queue.enqueue(lambda: None, payload_bytes=8)
        with self.assertRaises(ValueError):
            queue.enqueue(lambda: None, payload_bytes=-1)
        await queue.stop()
        self.assertEqual(2, len(self.values()))

    async def test_coroutine_cannot_split_a_transaction_across_awaits(self):
        async def unsupported():
            self.db.execute_write("INSERT INTO effects(value) VALUES ('wrong')")
        with self.assertRaises(TypeError):
            self.queue.enqueue(unsupported)
        self.assertEqual([], self.values())


if __name__ == "__main__":
    unittest.main()
