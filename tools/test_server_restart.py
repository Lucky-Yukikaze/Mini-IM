"""Verify real Qt recovery after a separate production server process is killed.

The server fixture pauses at configured commit, acknowledgement or file I/O
boundaries. This parent kills it, checks the surviving SQLite/file state and
starts a new process using exactly the same data directory and UDP port.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from test_native_flow import NativeClient
from protocol.pb import file_pb2

ROOT = Path(__file__).resolve().parents[1]


class ServerProcess:
    def __init__(self, process, log, evidence):
        self.process, self.log, self.evidence = process, log, evidence
        self.events = []
        self.condition = asyncio.Condition()
        self.finished = False
        self.hard_killed = False
        self.reader = asyncio.create_task(self.read())

    @classmethod
    async def start(cls, root, port, output, number, heartbeat):
        log = (output / f"server-{number}.log").open("wb")
        evidence = (output / f"server-{number}.jsonl").open("w", encoding="utf-8")
        env = {key: value for key, value in os.environ.items() if not key.startswith("MINIIM_")}
        process = await asyncio.create_subprocess_exec(sys.executable, "-u", str(ROOT / "tools/server_restart_fixture.py"),
            "--root", str(root), "--port", str(port), "--heartbeat", str(heartbeat), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=log, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        server = cls(process, log, evidence)
        try:
            ready = await server.wait("ready", timeout=12)
            server.port = ready["port"]
            return server
        except BaseException:
            await server.close(kill=True)
            raise

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                self.evidence.write(line.decode("utf-8"))
                self.evidence.flush()
                async with self.condition:
                    self.events.append(json.loads(line))
                    self.condition.notify_all()
        finally:
            async with self.condition:
                self.finished = True
                self.condition.notify_all()

    async def wait(self, event, predicate=lambda item: True, since=0, timeout=12):
        async with asyncio.timeout(timeout):
            async with self.condition:
                while True:
                    for item in self.events[since:]:
                        if item["event"] == event and predicate(item):
                            return item
                    if self.finished:
                        raise AssertionError(f"server stopped before {event}; {self.events[-3:]}")
                    await self.condition.wait()

    async def arm(self, point, operation, position=0):
        command = {"op": "arm", "checkpoint": {"point": point, "operation": operation, "position": position}}
        mark = len(self.events)
        self.process.stdin.write((json.dumps(command) + "\n").encode())
        await self.process.stdin.drain()
        await self.wait("armed", since=mark)

    async def close(self, kill=False):
        if self.log.closed:
            return
        try:
            if self.process.returncode is None:
                if kill:
                    self.hard_killed = True
                    self.process.kill()
                else:
                    self.process.stdin.write(b'{"op":"stop"}\n')
                    await self.process.stdin.drain()
                    self.process.stdin.close()
                await asyncio.wait_for(self.process.wait(), 5)
            if not kill and self.process.returncode != 0:
                raise AssertionError(f"server exited with {self.process.returncode}")
        finally:
            if self.process.returncode is None:
                self.hard_killed = True
                self.process.kill()
                await self.process.wait()
            self.process.stdin.close()
            try:
                await self.reader
            finally:
                self.log.close()
                self.evidence.close()


class ServerRestartTest(unittest.IsolatedAsyncioTestCase):
    driver_path: Path
    output_dir: Path

    async def asyncSetUp(self):
        self.output = self.output_dir / self._testMethodName
        self.output.mkdir()
        self.workspace = tempfile.TemporaryDirectory(prefix="fixture-", dir=self.output)
        self.root = Path(self.workspace.name)
        self.data = self.root / "server"
        self.db_path = self.data / "storage/sqlite/miniim.db"
        self.servers, self.clients = [], []
        self.addAsyncCleanup(self.cleanup)
        await self.start_server()
        self.endpoint = f"quic://127.0.0.1:{self.server.port}"
        self.initial = {}
        for user in ("alice", "bob"):
            client = await NativeClient.start(self.driver_path, self.output / f"{user}.log", self.root / f"state-{user}")
            self.clients.append(client)
            self.initial[user] = await client.connect(self.endpoint, user)
        self.alice, self.bob = self.clients
        mark = await self.alice.command("group", intent="restart-group", title="before", members=["bob"])
        self.conversation = (await self.alice.wait("conversation", since=mark))["conversationId"]
        await self.bob.wait("conversation", lambda item: item["conversationId"] == self.conversation)
        await self.alice.wait("control-writes", lambda item: not item["items"], since=mark)
        await self.check_sync()

    async def start_server(self):
        port = self.servers[0].port if self.servers else 0
        self.server = await ServerProcess.start(self.data, port, self.output, len(self.servers),
            heartbeat=0 if self._testMethodName == "test_idle_server_restart_uses_default_failure_detection" else 1)
        self.servers.append(self.server)
        if port:
            self.assertEqual(port, self.server.port)
            self.assertNotEqual(self.servers[-2].process.pid, self.server.process.pid)

    def rows(self, sql, params=(), path=None):
        with closing(sqlite3.connect(path or self.db_path, timeout=3)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, params)]

    def scalar(self, sql, params=()):
        return next(iter(self.rows(sql, params)[0].values()))

    async def until(self, predicate, timeout=12):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.02)

    async def check_sync(self):
        for user in ("alice", "bob"):
            expected = self.rows("SELECT seq,event_id FROM sync_events WHERE user_id=? ORDER BY seq", (user,))
            self.assertEqual(list(range(1, len(expected) + 1)), [row["seq"] for row in expected])
            cache = next((self.root / f"state-{user}").glob("*.sqlite"))
            await self.until(lambda: self.rows("SELECT value FROM metadata WHERE key='cursor'", path=cache)
                == [{"value": str(len(expected))}])
            self.assertEqual(expected, self.rows("SELECT position AS seq,event_id FROM seen ORDER BY position", path=cache))
        self.assertEqual("ok", self.scalar("PRAGMA integrity_check"))
        self.assertEqual([], self.rows("PRAGMA foreign_key_check"))

    async def kill_at_checkpoint(self):
        checkpoint = await self.server.wait("checkpoint", timeout=20)
        self.marks = [len(client.events) for client in self.clients]
        await self.server.close(kill=True)
        self.assertIsNotNone(self.server.process.returncode)
        self.assertNotEqual(0, self.server.process.returncode)
        return checkpoint

    def assert_replayed(self, operation, request_id, same_body=True):
        attempts = [item for server in self.servers for item in server.events
                    if item["event"] == "request" and item["operation"] == operation and item["requestId"] == request_id]
        self.assertGreaterEqual(len(attempts), 2)
        if same_body:
            self.assertEqual(1, len({item["body"] for item in attempts}))
        self.assertGreaterEqual(len({item["pid"] for item in attempts}), 2)
        return attempts

    async def check_new_sessions(self, timeout=18):
        for client, user, mark in zip(self.clients, ("alice", "bob"), self.marks):
            connected = await client.wait("connection", lambda item: item["state"] == "connected", since=mark, timeout=timeout)
            old = next(item["session"] for item in self.servers[0].events if item["event"] == "welcome" and item["user"] == user)
            self.assertNotEqual(old, connected["sessionId"])

    async def cleanup(self):
        errors = []
        for index, client in enumerate(self.clients):
            (self.output / f"client-{index}.json").write_text(json.dumps(client.events, ensure_ascii=False), encoding="utf-8")
            try:
                await client.close()
            except Exception as error:
                errors.append(error)
        for server in self.servers:
            try:
                await server.close(kill=bool(errors))
            except Exception as error:
                errors.append(error)
        (self.output / "lifecycle.json").write_text(json.dumps([
            dict(pid=server.process.pid, port=server.port, exitCode=server.process.returncode,
                 hardKilled=server.hard_killed) for server in self.servers], indent=2), encoding="utf-8")
        if self.db_path.exists():
            with closing(sqlite3.connect(self.db_path)) as db, closing(sqlite3.connect(self.output / "final-server.sqlite")) as copy:
                db.backup(copy)
        self.workspace.cleanup()
        if errors:
            raise errors[0]

    async def test_message_commit_survives_crash_before_ack_and_push(self):
        await self.server.arm("before-ack", "send_message")
        await self.alice.command("message", conversation=self.conversation, intent="committed-message", text="survives server")
        checkpoint = await self.kill_at_checkpoint()
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM messages"))
        await self.start_server()
        await self.alice.wait("message-sends", lambda item: not item["items"], since=self.marks[0], timeout=18)
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "committed-message", since=self.marks[1], timeout=18)
        self.assert_replayed("send_message", checkpoint["requestId"])
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM messages"))
        self.assertEqual(2, self.scalar("SELECT COUNT(*) FROM sync_events WHERE event_type='message'"))
        await self.check_new_sessions()
        await self.check_sync()

    async def rename_crash(self, point):
        before = self.scalar("SELECT COUNT(*) FROM sync_events")
        await self.server.arm(point, "rename_conversation")
        await self.alice.command("rename", conversation=self.conversation, title="after")
        checkpoint = await self.kill_at_checkpoint()
        committed = point == "before-ack"
        self.assertEqual("after" if committed else "before", self.scalar("SELECT title FROM conversations"))
        self.assertEqual(before + (2 if committed else 0), self.scalar("SELECT COUNT(*) FROM sync_events"))
        self.assertEqual(int(committed), self.scalar("SELECT COUNT(*) FROM control_write_results WHERE request_id=?", (checkpoint["requestId"],)))
        await self.start_server()
        await self.alice.wait("control-writes", lambda item: not item["items"], since=self.marks[0], timeout=18)
        await self.bob.wait("conversation", lambda item: item["title"] == "after", since=self.marks[1], timeout=18)
        self.assert_replayed("rename_conversation", checkpoint["requestId"])
        self.assertEqual(before + 2, self.scalar("SELECT COUNT(*) FROM sync_events"))
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM control_write_results WHERE request_id=?", (checkpoint["requestId"],)))
        await self.check_new_sessions()
        await self.check_sync()

    async def test_control_transaction_rolls_back_after_process_kill(self):
        await self.rename_crash("before-commit")

    async def test_control_commit_and_result_survive_lost_ack(self):
        await self.rename_crash("before-ack")

    async def upload_crash(self, point):
        payload = bytes(range(251)) * 1024
        source = self.root / "upload.bin"
        source.write_bytes(payload)
        await self.server.arm(point, "upload", position=65536)
        await self.alice.command("upload", conversation=self.conversation, path=str(source))
        checkpoint = await self.kill_at_checkpoint()
        row = self.rows("SELECT * FROM file_transfers")[0]
        stored = self.data / "storage/files" / row["storage_path"]
        if point == "file-flushed":
            self.assertGreater(stored.stat().st_size, row["received_bytes"])
            self.assertEqual(checkpoint["committed"], row["received_bytes"])
        else:
            self.assertEqual(stored.stat().st_size, row["received_bytes"])
        self.assertEqual(0, self.scalar("SELECT COUNT(*) FROM messages"))
        await self.start_server()
        await self.alice.wait("file-tasks", lambda item: not item["items"], since=self.marks[0], timeout=20)
        self.assertEqual(payload, stored.read_bytes())
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM file_transfers"))
        self.assertEqual("completed", self.scalar("SELECT status FROM file_transfers"))
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM messages"))
        self.assert_replayed("file_init", row["request_id"])
        resumed = next(item for item in self.server.events if item["event"] == "file-init")
        self.assertEqual(row["received_bytes"], resumed["offset"])
        await self.check_new_sessions()
        await self.check_sync()

    async def test_upload_uncommitted_disk_tail_is_not_counted_after_crash(self):
        await self.upload_crash("file-flushed")

    async def test_upload_committed_progress_resumes_after_crash(self):
        await self.upload_crash("file-progress")

    async def test_file_completion_transaction_rolls_back_and_retries(self):
        payload = b"atomic file completion" * 4096
        source = self.root / "complete.bin"
        source.write_bytes(payload)
        await self.server.arm("before-commit", "file_finish")
        await self.alice.command("upload", conversation=self.conversation, path=str(source))
        checkpoint = await self.kill_at_checkpoint()
        self.assertEqual("uploaded", self.scalar("SELECT status FROM file_transfers"))
        self.assertEqual(0, self.scalar("SELECT COUNT(*) FROM messages"))
        await self.start_server()
        await self.alice.wait("file-tasks", lambda item: not item["items"], since=self.marks[0], timeout=20)
        self.assert_replayed("file_finish", checkpoint["requestId"])
        self.assertEqual("completed", self.scalar("SELECT status FROM file_transfers"))
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM messages"))
        self.assertEqual(2, self.scalar("SELECT COUNT(*) FROM sync_events WHERE event_type='message'"))
        await self.check_sync()

    async def test_cancellation_commit_survives_server_restart(self):
        source = self.root / "cancel.bin"
        source.write_bytes(b"cancel after upload initialization" * 8192)
        # Cancel while waiting for an initialization ACK; the server has already committed the task.
        await self.server.arm("before-ack", "file_init")
        mark = await self.alice.command("upload", conversation=self.conversation, path=str(source))
        task = await self.alice.wait("file-tasks", lambda item: bool(item["items"]), since=mark)
        await self.kill_at_checkpoint()
        mark = await self.alice.command("cancel-file", intent=task["items"][0]["clientFileId"])
        await self.start_server()
        await self.server.arm("before-ack", "file_cancel")
        checkpoint = await self.kill_at_checkpoint()
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM file_cancellations"))
        self.assertEqual("cancelled", self.scalar("SELECT status FROM file_transfers"))
        await self.start_server()
        await self.alice.wait("file-tasks", lambda item: not item["items"], since=self.marks[0], timeout=20)
        self.assert_replayed("file_cancel", checkpoint["requestId"])
        self.assertEqual(0, self.scalar("SELECT COUNT(*) FROM messages"))
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM file_cancellations"))
        await self.check_sync()

    async def test_partial_download_survives_server_process_restart(self):
        payload = bytes(range(251)) * 4096
        source = self.root / "source.bin"
        source.write_bytes(payload)
        mark = await self.alice.command("upload", conversation=self.conversation, path=str(source))
        await self.alice.wait("file-tasks", lambda item: not item["items"], since=mark)
        file_id = self.scalar("SELECT file_id FROM file_transfers")
        target = self.root / "download.bin"
        target.write_bytes(b"preserve original destination")
        await self.server.arm("download-read", "download", position=65536)
        await self.bob.command("download", conversation=self.conversation, source=file_id, path=str(target))
        await self.server.wait("checkpoint")
        await self.until(lambda: any(path.stat().st_size >= 65536 for path in self.root.glob("download.bin.miniim-*.part")))
        partial = next(self.root.glob("download.bin.miniim-*.part")).stat().st_size
        await self.kill_at_checkpoint()
        self.assertEqual(b"preserve original destination", target.read_bytes())
        row = self.rows("SELECT * FROM file_transfers WHERE direction=2")[0]
        await self.start_server()
        await self.bob.wait("file-tasks", lambda item: not item["items"], since=self.marks[1], timeout=20)
        self.assertEqual(payload, target.read_bytes())
        attempts = self.assert_replayed("file_init", row["request_id"], same_body=False)
        bodies = [file_pb2.FileInit.FromString(bytes.fromhex(item["body"])) for item in attempts]
        for body in bodies:
            self.assertEqual((self.conversation, row["client_file_id"], file_id, 2),
                             (body.conversation_id, body.client_file_id, body.source_file_id, body.direction))
        resumed = next(item for item in self.server.events if item["event"] == "file-init" and item["direction"] == 2)
        self.assertEqual(partial, resumed["requestedOffset"])
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM file_transfers WHERE direction=2"))
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM messages"))
        await self.check_new_sessions()
        await self.check_sync()


    async def test_message_uncommitted_transaction_is_rolled_back(self):
        before = self.scalar("SELECT COUNT(*) FROM sync_events")
        await self.server.arm("before-commit", "send_message")
        await self.alice.command("message", conversation=self.conversation, intent="uncommitted", text="retry me")
        checkpoint = await self.kill_at_checkpoint()
        for table in ("messages", "message_deliveries", "message_read_counters"):
            self.assertEqual(0, self.scalar("SELECT COUNT(*) FROM " + table))
        self.assertEqual(before, self.scalar("SELECT COUNT(*) FROM sync_events"))
        await self.start_server()
        await self.alice.wait("message-sends", lambda item: not item["items"], since=self.marks[0], timeout=18)
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "uncommitted", since=self.marks[1], timeout=18)
        self.assert_replayed("send_message", checkpoint["requestId"])
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM messages"))
        self.assertEqual(before + 2, self.scalar("SELECT COUNT(*) FROM sync_events"))
        await self.check_sync()

    async def test_file_completion_commit_survives_lost_confirmation(self):
        payload = b"published file survives" * 4096
        source = self.root / "published.bin"
        source.write_bytes(payload)
        await self.server.arm("before-ack", "file_finish")
        await self.alice.command("upload", conversation=self.conversation, path=str(source))
        checkpoint = await self.kill_at_checkpoint()
        self.assertEqual("completed", self.scalar("SELECT status FROM file_transfers"))
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM messages"))
        before = self.rows("SELECT seq,event_id FROM sync_events ORDER BY user_id,seq")
        stored = self.data / "storage/files" / self.scalar("SELECT storage_path FROM file_transfers")
        await self.start_server()
        await self.alice.wait("file-tasks", lambda item: not item["items"], since=self.marks[0], timeout=18)
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "file-msg-" + checkpoint["entityId"],
                            since=self.marks[1], timeout=18)
        self.assertEqual(before, self.rows("SELECT seq,event_id FROM sync_events ORDER BY user_id,seq"))
        self.assertEqual(payload, stored.read_bytes())
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM messages"))
        await self.check_sync()

    async def test_receipt_and_recall_commits_survive_lost_confirmations(self):
        mark = await self.alice.command("message", conversation=self.conversation, intent="state-message", text="redact later")
        message = await self.bob.wait("message", lambda item: item["clientMsgId"] == "state-message")
        await self.alice.wait("message-sends", lambda item: not item["items"], since=mark)
        await self.server.arm("before-ack", "receipt")
        await self.bob.command("receipt", conversation=self.conversation, seq=message["seq"])
        checkpoint = await self.kill_at_checkpoint()
        self.assertEqual(1, self.scalar("SELECT last_read_seq FROM conversation_members WHERE user_id='bob'"))
        await self.start_server()
        await self.bob.wait("control-writes", lambda item: not item["items"], since=self.marks[1], timeout=18)
        await self.alice.wait("update", lambda item: item["type"] == "receipt" and item["readerId"] == "bob",
                              since=self.marks[0], timeout=18)
        self.assert_replayed("receipt", checkpoint["requestId"])
        self.assertEqual(2, self.scalar("SELECT COUNT(*) FROM sync_events WHERE event_type='receipt'"))
        await self.server.arm("before-ack", "recall")
        await self.alice.command("recall", conversation=self.conversation, message=message["id"])
        checkpoint = await self.kill_at_checkpoint()
        self.assertEqual(1, self.scalar("SELECT recalled FROM messages"))
        await self.start_server()
        await self.alice.wait("control-writes", lambda item: not item["items"], since=self.marks[0], timeout=18)
        await self.bob.wait("update", lambda item: item["type"] == "recall" and item["messageId"] == message["id"],
                            since=self.marks[1], timeout=18)
        self.assert_replayed("recall", checkpoint["requestId"])
        self.assertEqual(2, self.scalar("SELECT COUNT(*) FROM sync_events WHERE event_type='recall'"))
        await self.check_sync()
        for user in ("alice", "bob"):
            cache = next((self.root / f"state-{user}").glob("*.sqlite"))
            stored = json.loads(self.rows("SELECT data FROM objects WHERE kind='message' AND id=?", (message["id"],), cache)[0]["data"])
            self.assertTrue(stored["recalled"])
            self.assertEqual("", stored["text"])

    async def test_idle_server_restart_uses_default_failure_detection(self):
        self.marks = [len(client.events) for client in self.clients]
        await self.server.close(kill=True)
        await self.start_server()
        await self.check_new_sessions(timeout=40)
        await self.check_sync()
        mark = await self.alice.command("message", conversation=self.conversation, intent="after-idle-crash", text="connected again")
        await self.alice.wait("message-sends", lambda item: not item["items"], since=mark)
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "after-idle-crash", since=self.marks[1])
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM messages"))
        await self.check_sync()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", type=Path, default=ROOT / "build/client_qt611/Release/mini_im_native_driver.exe")
    parser.add_argument("--output", type=Path, default=ROOT / "tmp/server-restart")
    parser.add_argument("--test", action="append")
    args = parser.parse_args()
    if not args.client.is_file():
        parser.error("build mini_im_native_driver first")
    output = args.output.resolve() / time.strftime("%Y%m%d-%H%M%S")
    output.mkdir(parents=True)
    ServerRestartTest.driver_path = args.client.resolve()
    ServerRestartTest.output_dir = output
    names = args.test or unittest.defaultTestLoader.getTestCaseNames(ServerRestartTest)
    for name in names:
        if not name.startswith("test_") or not callable(getattr(ServerRestartTest, name, None)):
            parser.error(f"unknown test: {name}")
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(ServerRestartTest(name) for name in names))
    (output / "results.json").write_text(json.dumps(dict(tests=result.testsRun, failures=len(result.failures),
        errors=len(result.errors), successful=result.wasSuccessful(), client=str(args.client.resolve()),
        scope="Separate production server process hard kills + real Qt native clients; no Vue UI"), indent=2), encoding="utf-8")
    print(f"Server restart evidence: {output}")
    raise SystemExit(not result.wasSuccessful())


if __name__ == "__main__":
    main()
