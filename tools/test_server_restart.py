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
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import unittest

from test_native_flow import NativeClient
from aioquic.asyncio import connect
from aioquic.quic.configuration import QuicConfiguration
from protocol.codec import EnvelopeCodec
from protocol.pb import common_pb2, envelope_pb2, file_pb2, message_pb2, sync_pb2

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
        self.joint_crashes = []
        self.restart_all_clients = False
        self.clients_need_restart = False
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
        if self.clients_need_restart:
            old_clients = list(self.clients)
            self.marks = [0, 0]
            restored = {}
            for index, user in enumerate(("alice", "bob")):
                client = await NativeClient.start(self.driver_path,
                    self.output / f"{user}-restart-{len(self.servers)}.log", self.root / f"state-{user}")
                self.clients[index] = client
                self.assertNotEqual(old_clients[index].process.pid, client.process.pid)
                restored[user] = await client.connect(self.endpoint, user)
                self.assert_restored_queue_identity(user, restored[user])
                self.joint_crashes[-1]["clients"][index]["replacementPid"] = client.process.pid
            self.alice, self.bob = self.clients
            self.clients_need_restart = False
            (self.output / f"joint-initial-{len(self.servers)}.json").write_text(
                json.dumps(restored, ensure_ascii=False), encoding="utf-8")
            await self.check_new_sessions()

    def assert_restored_queue_identity(self, user, initial):
        cache = self.output / f"{user}-before-crash-{len(self.servers) - 1}.sqlite"
        for table, key, columns, fields, terminal in (
            ("message_outbox", "messageSends", "request_id,client_msg_id", ("requestId", "clientMsgId"), "'confirmed'"),
            ("control_outbox", "controlWrites", "request_id,operation", ("requestId", "operation"), "'confirmed'"),
            ("file_tasks", "fileTasks", "id,init_request,finish_request,cancel_request",
             ("clientFileId", "requestId", "finishRequestId", "cancelRequestId"),
             "'completed','cancelled'"),
        ):
            persisted = self.rows(f"SELECT {columns} FROM {table} WHERE status NOT IN ({terminal})", path=cache)
            self.assertEqual({tuple(row.values()) for row in persisted},
                             {tuple(item.get(field, "") for field in fields) for item in initial[key]})
        cursor = self.rows("SELECT value FROM metadata WHERE key='cursor'", path=cache)
        self.assertGreaterEqual(int(initial["globalCursor"]), int(cursor[0]["value"]) if cursor else 0)

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
        def confirmations_settled():
            for user in ("alice", "bob"):
                cache = next((self.root / f"state-{user}").glob("*.sqlite"))
                metadata = {row["key"]: row["value"] for row in self.rows("SELECT key,value FROM metadata", path=cache)}
                maximum = self.scalar("SELECT COALESCE(MAX(seq),0) FROM sync_events WHERE user_id=?", (user,))
                if int(metadata.get("sync_confirmed_cursor", 0)) != maximum or metadata.get("sync_confirmation"):
                    return False
            return True
        await self.until(confirmations_settled, timeout=18)
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
        if self.restart_all_clients:
            self.assertEqual(2, len(self.clients))
            await asyncio.gather(*(client.crash() for client in self.clients))
            record = dict(serverPid=self.server.process.pid, checkpoint=checkpoint, clients=[])
            for user, client in zip(("alice", "bob"), self.clients):
                self.assertNotEqual(0, client.process.returncode)
                record["clients"].append(dict(user=user, pid=client.process.pid, exitCode=client.process.returncode))
                (self.output / f"{user}-before-crash-{len(self.servers)}.json").write_text(
                    json.dumps(client.events, ensure_ascii=False), encoding="utf-8")
                cache = next((self.root / f"state-{user}").glob("*.sqlite"))
                with closing(sqlite3.connect(cache)) as db, closing(sqlite3.connect(
                        self.output / f"{user}-before-crash-{len(self.servers)}.sqlite")) as copy:
                    db.backup(copy)
            self.joint_crashes.append(record)
            self.clients_need_restart = True
        return checkpoint

    async def assert_replayed(self, operation, request_id, same_body=True):
        # Client acknowledgements and server stdout are collected independently.
        await self.server.wait("request", lambda item: item["operation"] == operation and item["requestId"] == request_id)
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
                if self.restart_all_clients:
                    user = ("alice", "bob")[index]
                    cache = next((self.root / f"state-{user}").glob("*.sqlite"))
                    with closing(sqlite3.connect(cache)) as db, closing(sqlite3.connect(
                            self.output / f"final-{user}.sqlite")) as copy:
                        db.backup(copy)
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
        (self.output / "joint-crashes.json").write_text(json.dumps(self.joint_crashes, indent=2), encoding="utf-8")
        if self.db_path.exists():
            with closing(sqlite3.connect(self.db_path)) as db, closing(sqlite3.connect(self.output / "final-server.sqlite")) as copy:
                db.backup(copy)
        self.workspace.cleanup()
        if errors:
            raise errors[0]


    async def delivery_confirmation_crash(self, point):
        await self.server.arm(point, "sync_applied")
        await self.alice.command("message", conversation=self.conversation, intent="confirm-crash", text="delivery proof")
        checkpoint = await self.kill_at_checkpoint()
        message = self.rows("SELECT server_msg_id FROM messages WHERE client_msg_id='confirm-crash'")[0]["server_msg_id"]
        committed = point == "before-ack"
        before = self.rows("SELECT * FROM message_deliveries WHERE user_id='bob' AND server_msg_id=?", (message,))[0]
        committed_ack = self.scalar("SELECT ack FROM control_write_results WHERE request_id=?",
                                    (checkpoint["requestId"],)) if committed else None
        if self.restart_all_clients:
            cache = self.output / f"bob-before-crash-{len(self.servers)}.sqlite"
            metadata = {row["key"]: row["value"] for row in self.rows("SELECT key,value FROM metadata", path=cache)}
            pending = json.loads(metadata["sync_confirmation"])
            self.assertEqual(checkpoint["requestId"], pending["requestId"])
            request = next(item for item in self.server.events if item["event"] == "request"
                           and item["requestId"] == checkpoint["requestId"])
            body = sync_pb2.SyncApplied.FromString(bytes.fromhex(request["body"]))
            self.assertEqual(int(pending["cursor"]), body.global_cursor)
            self.assertLessEqual(int(pending["cursor"]), int(metadata["cursor"]))
            self.assertGreater(int(pending["cursor"]), int(metadata.get("sync_confirmed_cursor", 0)))
        self.assertEqual("delivered" if committed else "sent", before["status"])
        self.assertEqual(committed, before["delivered_at_ms"] is not None)
        self.assertEqual(int(committed), self.scalar(
            "SELECT COUNT(*) FROM control_write_results WHERE request_id=?", (checkpoint["requestId"],)))
        self.assertEqual(2 if committed else 0, self.scalar(
            "SELECT COUNT(*) FROM sync_events WHERE event_type='delivery_updated'"))
        await self.start_server()
        await self.alice.wait("update", lambda item: item["type"] == "delivery" and item["messageId"] == message,
                              since=self.marks[0], timeout=18)
        await self.check_new_sessions()
        await self.check_sync()
        await self.assert_replayed("sync_applied", checkpoint["requestId"])
        replay_ack = await self.server.wait("durable-ack", lambda item: item["requestId"] == checkpoint["requestId"])
        saved_ack = self.scalar("SELECT ack FROM control_write_results WHERE request_id=?", (checkpoint["requestId"],))
        self.assertEqual(saved_ack.hex(), replay_ack["payload"])
        if committed:
            self.assertEqual(committed_ack, saved_ack)
        after = self.rows("SELECT * FROM message_deliveries WHERE user_id='bob' AND server_msg_id=?", (message,))[0]
        self.assertEqual("delivered", after["status"])
        if committed:
            self.assertEqual(before, after)
        self.assertEqual(2, self.scalar("SELECT COUNT(*) FROM sync_events WHERE event_type='delivery_updated'"))
        self.assertIsNone(after["read_at_ms"])

    async def test_delivery_confirmation_rolls_back_before_server_commit(self):
        await self.delivery_confirmation_crash("before-commit")

    async def test_delivery_confirmation_commit_survives_lost_ack(self):
        await self.delivery_confirmation_crash("before-ack")

    async def test_message_commit_survives_crash_before_ack_and_push(self):
        await self.server.arm("before-ack", "send_message")
        await self.alice.command("message", conversation=self.conversation, intent="committed-message", text="survives server")
        checkpoint = await self.kill_at_checkpoint()
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM messages"))
        committed_ack = self.scalar("SELECT ack FROM control_write_results WHERE user_id='alice' AND request_id=?",
                                    (checkpoint["requestId"],))
        await self.start_server()
        await self.alice.wait("message-sends", lambda item: not item["items"], since=self.marks[0], timeout=18)
        replayed_ack = await self.server.wait("message-ack", lambda item: item["requestId"] == checkpoint["requestId"])
        self.assertEqual(committed_ack.hex(), replayed_ack["payload"])
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "committed-message", since=self.marks[1], timeout=18)
        await self.assert_replayed("send_message", checkpoint["requestId"])
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
        await self.assert_replayed("rename_conversation", checkpoint["requestId"])
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
        await self.assert_replayed("file_init", row["request_id"])
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
        self.assertEqual(0, self.scalar("SELECT COUNT(*) FROM control_write_results WHERE operation='file_finish'"))
        await self.start_server()
        await self.alice.wait("file-tasks", lambda item: not item["items"], since=self.marks[0], timeout=20)
        await self.assert_replayed("file_finish", checkpoint["requestId"])
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
        await self.assert_replayed("file_cancel", checkpoint["requestId"])
        self.assertEqual(0, self.scalar("SELECT COUNT(*) FROM messages"))
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM file_cancellations"))
        await self.check_sync()

    async def cancellation_joint_crash(self, point):
        source = self.root / "cancel-joint.bin"
        source.write_bytes(b"cancel original file task" * 4096)
        await self.server.arm("before-ack", "file_init")
        mark = await self.alice.command("upload", conversation=self.conversation, path=str(source))
        pending = await self.alice.wait("file-tasks", lambda item: bool(item["items"]), since=mark)
        intent = pending["items"][0]["clientFileId"]
        await self.kill_at_checkpoint()
        # Stop automatic reconnect, save cancellation offline, then arm the next service before connecting.
        mark = await self.alice.command("disconnect")
        await self.alice.wait("connection", lambda item: item["state"] == "disconnected", since=mark)
        mark = await self.alice.command("cancel-file", intent=intent)
        cancelling = await self.alice.wait("file-tasks", lambda item: any(task["status"] == "cancelling"
            for task in item["items"]), since=mark)
        request_id = cancelling["items"][0]["cancelRequestId"]
        original = self.rows("SELECT * FROM file_transfers")[0]
        before_events = self.rows("SELECT seq,event_id,payload FROM sync_events WHERE event_type='file_updated' ORDER BY user_id,seq")
        await self.start_server()
        await self.server.arm(point, "file_cancel")
        await self.alice.connect(self.endpoint, "alice")
        self.restart_all_clients = True
        checkpoint = await self.kill_at_checkpoint()
        self.assertEqual(request_id, checkpoint["requestId"])
        committed = point == "before-ack"
        self.assertEqual(int(committed), self.scalar("SELECT COUNT(*) FROM file_cancellations"))
        self.assertEqual(int(committed), self.scalar("SELECT COUNT(*) FROM control_write_results WHERE operation='file_cancel'"))
        stopped = self.rows("SELECT * FROM file_transfers")[0]
        if committed:
            self.assertEqual("cancelled", stopped["status"])
        else:
            self.assertEqual(original, stopped)
            self.assertEqual(before_events, self.rows(
                "SELECT seq,event_id,payload FROM sync_events WHERE event_type='file_updated' ORDER BY user_id,seq"))
        committed_ack = self.scalar("SELECT ack FROM control_write_results WHERE request_id=?", (request_id,)) if committed else None
        self.assertEqual(0, self.scalar("SELECT COUNT(*) FROM messages"))
        cache = self.output / f"alice-before-crash-{len(self.servers)}.sqlite"
        saved = self.rows("SELECT id,cancel_request,status FROM file_tasks", path=cache)[0]
        self.assertEqual(dict(id=intent, cancel_request=request_id, status="cancelling"), saved)
        await self.start_server()
        await self.alice.wait("file-tasks", lambda item: not item["items"], since=self.marks[0], timeout=20)
        await self.assert_replayed("file_cancel", request_id)
        replay_ack = await self.server.wait("durable-ack", lambda item: item["requestId"] == request_id)
        result = self.scalar("SELECT ack FROM control_write_results WHERE request_id=?", (request_id,))
        self.assertEqual(result.hex(), replay_ack["payload"])
        final = self.rows("SELECT * FROM file_transfers")[0]
        self.assertEqual("cancelled", final["status"])
        self.assertEqual(original["file_id"], final["file_id"])
        self.assertEqual(original["received_bytes"], final["received_bytes"])
        if committed:
            self.assertEqual(committed_ack, result)
            self.assertEqual(stopped, final)
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM file_cancellations"))
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM control_write_results WHERE operation='file_cancel'"))
        self.assertEqual(0, self.scalar("SELECT COUNT(*) FROM messages"))
        self.assertEqual(len(before_events) + 2, self.scalar("SELECT COUNT(*) FROM sync_events WHERE event_type='file_updated'"))
        await self.check_sync()

    async def test_joint_crash_cancel_before_commit(self):
        await self.cancellation_joint_crash("before-commit")

    async def test_joint_crash_cancel_after_commit(self):
        await self.cancellation_joint_crash("before-ack")

    async def test_joint_crash_delivery_before_commit(self):
        self.restart_all_clients = True
        await self.delivery_confirmation_crash("before-commit")

    async def test_joint_crash_delivery_after_commit(self):
        self.restart_all_clients = True
        await self.delivery_confirmation_crash("before-ack")

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
        attempts = await self.assert_replayed("file_init", row["request_id"], same_body=False)
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
        self.assertEqual(0, self.scalar("SELECT COUNT(*) FROM control_write_results WHERE request_id=?",
                                        (checkpoint["requestId"],)))
        await self.start_server()
        await self.alice.wait("message-sends", lambda item: not item["items"], since=self.marks[0], timeout=18)
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "uncommitted", since=self.marks[1], timeout=18)
        await self.assert_replayed("send_message", checkpoint["requestId"])
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM messages"))
        self.assertEqual(before + 2, self.scalar("SELECT COUNT(*) FROM sync_events"))
        await self.check_sync()

    async def replay_completed_file_request(self, request_id):
        original = next(item for server in self.servers for item in server.events
            if item["event"] == "request" and item["operation"] == "file_finish" and item["requestId"] == request_id)
        config = QuicConfiguration(is_client=True, alpn_protocols=["mini-im"])
        config.verify_mode = ssl.CERT_NONE
        async with connect("127.0.0.1", self.server.port, configuration=config) as protocol:
            reader, writer = await protocol.create_stream()

            async def receive(body):
                async with asyncio.timeout(5):
                    while True:
                        size = struct.unpack(">I", await reader.readexactly(4))[0]
                        response = EnvelopeCodec.decode(await reader.readexactly(size))
                        if response.HasField(body):
                            return response

            hello = envelope_pb2.Envelope(version=1, request_id="replay-hello", channel=common_pb2.CHANNEL_CONTROL)
            hello.hello.token = "dev-token:alice"
            hello.hello.device_id = "finish-replay-device"
            writer.write(EnvelopeCodec.encode_frame(hello))
            welcome = await receive("welcome")
            request = envelope_pb2.Envelope(version=1, request_id=request_id, channel=common_pb2.CHANNEL_FILE,
                session_id=welcome.welcome.session_id, device_id=hello.hello.device_id)
            request.file_finish.ParseFromString(bytes.fromhex(original["body"]))
            writer.write(EnvelopeCodec.encode_frame(request))
            response = await receive("ack")
            self.assertEqual(request_id, response.request_id)
            writer.close()
            return response.ack.SerializeToString()

    async def test_file_completion_commit_survives_lost_confirmation(self):
        payload = b"published file survives" * 4096
        source = self.root / "published.bin"
        source.write_bytes(payload)
        await self.server.arm("before-ack", "file_finish")
        await self.alice.command("upload", conversation=self.conversation, path=str(source))
        checkpoint = await self.kill_at_checkpoint()
        self.assertEqual("completed", self.scalar("SELECT status FROM file_transfers"))
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM messages"))
        committed_ack = self.scalar("SELECT ack FROM control_write_results WHERE user_id='alice' AND request_id=?",
                                    (checkpoint["requestId"],))
        before = self.rows("SELECT seq,event_id FROM sync_events ORDER BY user_id,seq")
        stored = self.data / "storage/files" / self.scalar("SELECT storage_path FROM file_transfers")
        await self.start_server()
        await self.alice.wait("file-tasks", lambda item: not item["items"], since=self.marks[0], timeout=18)
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "file-msg-" + checkpoint["entityId"],
                            since=self.marks[1], timeout=18)
        self.assertEqual(before, self.rows("SELECT seq,event_id FROM sync_events ORDER BY user_id,seq"))
        self.assertEqual(payload, stored.read_bytes())
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM messages"))
        # The Qt upload may finish through sync without resending FileFinish; exercise the saved request explicitly.
        self.assertEqual(committed_ack, await self.replay_completed_file_request(checkpoint["requestId"]))
        replayed_ack = await self.server.wait("finish-ack", lambda item: item["requestId"] == checkpoint["requestId"])
        self.assertEqual(committed_ack.hex(), replayed_ack["payload"])
        await self.assert_replayed("file_finish", checkpoint["requestId"])
        await self.check_sync()

    async def test_receipt_and_recall_commits_survive_lost_confirmations(self):
        mark = await self.alice.command("message", conversation=self.conversation, intent="state-message", text="redact later")
        message = await self.bob.wait("message", lambda item: item["clientMsgId"] == "state-message")
        await self.alice.wait("message-sends", lambda item: not item["items"], since=mark)
        await self.server.arm("before-ack", "receipt")
        await self.bob.command("receipt", conversation=self.conversation, seq=message["seq"])
        checkpoint = await self.kill_at_checkpoint()
        self.assertEqual(1, self.scalar("SELECT last_read_seq FROM conversation_members WHERE user_id='bob'"))
        self.assertEqual(0, self.scalar("SELECT unread_count FROM message_read_counters"))
        committed_counts = self.rows("SELECT event_id,user_id,seq,payload FROM sync_events "
            "WHERE event_type='read_count_updated' ORDER BY user_id")
        self.assertEqual(2, len(committed_counts))
        await self.start_server()
        await self.bob.wait("control-writes", lambda item: not item["items"], since=self.marks[1], timeout=18)
        await self.alice.wait("update", lambda item: item["type"] == "receipt" and item["readerId"] == "bob",
                              since=self.marks[0], timeout=18)
        await self.assert_replayed("receipt", checkpoint["requestId"])
        self.assertEqual(2, self.scalar("SELECT COUNT(*) FROM sync_events WHERE event_type='receipt'"))
        await self.alice.wait("update", lambda item: item["type"] == "readCount" and
            item["messageId"] == message["id"] and item["unreadCount"] == 0, since=self.marks[0])
        self.assertEqual(committed_counts, self.rows("SELECT event_id,user_id,seq,payload FROM sync_events "
            "WHERE event_type='read_count_updated' ORDER BY user_id"))
        await self.server.arm("before-ack", "recall")
        await self.alice.command("recall", conversation=self.conversation, message=message["id"])
        checkpoint = await self.kill_at_checkpoint()
        self.assertEqual(1, self.scalar("SELECT recalled FROM messages"))
        await self.start_server()
        await self.alice.wait("control-writes", lambda item: not item["items"], since=self.marks[0], timeout=18)
        await self.bob.wait("update", lambda item: item["type"] == "recall" and item["messageId"] == message["id"],
                            since=self.marks[1], timeout=18)
        await self.assert_replayed("recall", checkpoint["requestId"])
        self.assertEqual(2, self.scalar("SELECT COUNT(*) FROM sync_events WHERE event_type='recall'"))
        await self.check_sync()
        for user in ("alice", "bob"):
            cache = next((self.root / f"state-{user}").glob("*.sqlite"))
            stored = json.loads(self.rows("SELECT data FROM objects WHERE kind='message' AND id=?", (message["id"],), cache)[0]["data"])
            self.assertTrue(stored["recalled"])
            self.assertEqual("", stored["text"])
            count = json.loads(self.rows("SELECT data FROM objects WHERE kind='readCount' AND id=?",
                (message["id"],), cache)[0]["data"])
            self.assertEqual(0, count["unreadCount"])

    async def test_read_count_transaction_rolls_back_before_server_commit(self):
        mark = await self.alice.command("message", conversation=self.conversation, intent="count-rollback", text="unread")
        message = await self.bob.wait("message", lambda item: item["clientMsgId"] == "count-rollback")
        await self.alice.wait("message-sends", lambda item: not item["items"], since=mark)
        await self.check_sync()
        await self.server.arm("before-commit", "receipt")
        await self.bob.command("receipt", conversation=self.conversation, seq=message["seq"])
        checkpoint = await self.kill_at_checkpoint()
        self.assertEqual(0, self.scalar("SELECT last_read_seq FROM conversation_members WHERE user_id='bob'"))
        self.assertEqual(1, self.scalar("SELECT unread_count FROM message_read_counters"))
        self.assertEqual(0, self.scalar("SELECT COUNT(*) FROM sync_events WHERE event_type='read_count_updated'"))
        self.assertEqual(0, self.scalar("SELECT COUNT(*) FROM control_write_results WHERE request_id=?", (checkpoint["requestId"],)))
        await self.start_server()
        await self.alice.wait("update", lambda item: item["type"] == "readCount" and
            item["messageId"] == message["id"] and item["unreadCount"] == 0, since=self.marks[0], timeout=18)
        await self.assert_replayed("receipt", checkpoint["requestId"])
        self.assertEqual(2, self.scalar("SELECT COUNT(*) FROM sync_events WHERE event_type='read_count_updated'"))
        await self.check_sync()

    async def init_identity_crash(self, point):
        self.restart_all_clients = True
        payload = b"persistent initialization request" * 4096
        source = self.root / "init-identity.bin"
        source.write_bytes(payload)
        before = self.scalar("SELECT COUNT(*) FROM sync_events")
        await self.server.arm(point, "file_init")
        await self.alice.command("upload", conversation=self.conversation, path=str(source))
        checkpoint = await self.kill_at_checkpoint()
        committed = point == "before-ack"
        self.assertEqual(int(committed), self.scalar("SELECT COUNT(*) FROM file_transfers"))
        bindings = self.rows("SELECT * FROM file_init_requests")
        self.assertEqual(int(committed), len(bindings))
        self.assertEqual(before + (2 if committed else 0), self.scalar("SELECT COUNT(*) FROM sync_events"))
        await self.start_server()
        await self.alice.wait("file-tasks", lambda data: not data["items"], since=0, timeout=20)
        await self.assert_replayed("file_init", checkpoint["requestId"])
        after = self.rows("SELECT * FROM file_init_requests")
        self.assertEqual(1, len(after))
        self.assertEqual(checkpoint["requestId"], after[0]["request_id"])
        if committed:
            self.assertEqual(bindings, after)
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM file_transfers"))
        self.assertEqual("completed", self.scalar("SELECT status FROM file_transfers"))
        stored = self.data / "storage/files" / self.scalar("SELECT storage_path FROM file_transfers")
        self.assertEqual(payload, stored.read_bytes())
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM messages"))
        await self.check_sync()

    async def test_init_identity_rolls_back_before_joint_crash(self):
        await self.init_identity_crash("before-commit")

    async def test_init_identity_commit_survives_joint_crash(self):
        await self.init_identity_crash("before-ack")

    async def test_joint_crash_message_before_commit(self):
        self.restart_all_clients = True
        await self.test_message_uncommitted_transaction_is_rolled_back()

    async def test_joint_crash_message_after_commit(self):
        self.restart_all_clients = True
        await self.test_message_commit_survives_crash_before_ack_and_push()

    async def test_joint_crash_upload_uncommitted_tail(self):
        self.restart_all_clients = True
        await self.test_upload_uncommitted_disk_tail_is_not_counted_after_crash()

    async def test_joint_crash_upload_committed_progress(self):
        self.restart_all_clients = True
        await self.test_upload_committed_progress_resumes_after_crash()

    async def test_joint_crash_partial_download(self):
        self.restart_all_clients = True
        await self.test_partial_download_survives_server_process_restart()

    async def test_joint_crash_completion_before_commit(self):
        self.restart_all_clients = True
        await self.test_file_completion_transaction_rolls_back_and_retries()

    async def test_joint_crash_completion_after_commit(self):
        self.restart_all_clients = True
        await self.test_file_completion_commit_survives_lost_confirmation()

    async def test_joint_crash_control_before_commit(self):
        self.restart_all_clients = True
        await self.test_control_transaction_rolls_back_after_process_kill()

    async def test_joint_crash_control_after_commit(self):
        self.restart_all_clients = True
        await self.test_control_commit_and_result_survive_lost_ack()

    async def test_joint_crash_receipt_and_recall(self):
        self.restart_all_clients = True
        await self.test_receipt_and_recall_commits_survive_lost_confirmations()

    def burn_delivery(self, message, user):
        return self.rows("SELECT * FROM message_deliveries WHERE server_msg_id=? AND user_id=?",
                         (message, user))[0]

    def cached_message(self, user, message):
        cache = next((self.root / f"state-{user}").glob("*.sqlite"))
        rows = self.rows("SELECT data FROM objects WHERE kind='message' AND id=?", (message,), cache)
        return json.loads(rows[0]["data"]) if rows else None

    def assert_burn_copies(self, message, user, burned):
        rows = self.rows("SELECT payload FROM sync_events WHERE event_type='message' AND user_id=?", (user,))
        copies = [message_pb2.Message.FromString(row["payload"]) for row in rows]
        copies = [copy for copy in copies if copy.message_id == message]
        self.assertEqual(1, len(copies))
        self.assertEqual(burned, copies[0].recalled)
        self.assertEqual(b"" if burned else b"private across restart", copies[0].content)

    async def check_burn_device_sync(self, cache_user):
        cache = next((self.root / f"state-{cache_user}").glob("*.sqlite"))
        maximum = self.scalar("SELECT MAX(seq) FROM sync_events WHERE user_id='bob'")
        await self.until(lambda: self.rows("SELECT value FROM metadata WHERE key='sync_confirmed_cursor'", path=cache)
                         == [{"value": str(maximum)}])
        self.assertEqual(self.rows("SELECT seq,event_id FROM sync_events WHERE user_id='bob' ORDER BY seq"),
                         self.rows("SELECT position AS seq,event_id FROM seen ORDER BY position", path=cache))

    async def burn_crash(self, point):
        await self.server.arm(point, "burn")
        mark = await self.alice.command("message", conversation=self.conversation, intent="burn-restart",
                                        text="private across restart", burnMode=1, burnTtlSec=5)
        message = await self.bob.wait("message", lambda item: item["clientMsgId"] == "burn-restart")
        message_id = message["id"]
        await self.alice.wait("message-sends", lambda item: not item["items"], since=mark)
        await self.check_sync()
        sender = self.burn_delivery(message_id, "alice")
        self.assertEqual(5000, sender["burn_at_ms"] - sender["burn_started_at_ms"])
        self.assertIsNone(self.burn_delivery(message_id, "bob")["burn_at_ms"])
        checkpoint = await self.kill_at_checkpoint()
        self.assertEqual("burn", checkpoint["operation"])
        committed = point == "after-commit"
        self.assertEqual(committed, self.burn_delivery(message_id, "alice")["burned_at_ms"] is not None)
        self.assertEqual(int(committed), self.scalar("SELECT COUNT(*) FROM sync_events WHERE event_type='recall'"))
        self.assert_burn_copies(message_id, "alice", committed)
        self.assert_burn_copies(message_id, "bob", False)
        self.assertEqual(b"private across restart", self.scalar("SELECT content FROM messages"))
        if committed:
            self.assertEqual(checkpoint["eventIds"], [row["event_id"] for row in self.rows(
                "SELECT event_id FROM sync_events WHERE event_type='recall'")])
        before = self.burn_delivery(message_id, "alice")
        await self.start_server()
        await self.check_new_sessions()
        await self.check_sync()
        await self.until(lambda: self.cached_message("alice", message_id)["recalled"])
        self.assertEqual("", self.cached_message("alice", message_id)["text"])
        self.assertEqual("private across restart", self.cached_message("bob", message_id)["text"])
        after = self.burn_delivery(message_id, "alice")
        self.assertEqual(sender["burn_at_ms"], after["burn_at_ms"])
        self.assertGreaterEqual(after["burned_at_ms"], after["burn_at_ms"])
        if committed:
            self.assertEqual(before, after)
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM sync_events WHERE event_type='recall'"))
        self.assert_burn_copies(message_id, "alice", True)
        self.assert_burn_copies(message_id, "bob", False)

        # A second device shares the first user's read deadline; duplicate reads cannot restart it.
        second = await NativeClient.start(self.driver_path, self.output / "bob-second.log", self.root / "state-bob-second")
        self.clients.append(second)
        await second.connect(self.endpoint, "bob", "device-bob-second")
        await self.until(lambda: self.cached_message("bob-second", message_id) is not None)
        mark = await self.bob.command("receipt", conversation=self.conversation, seq=message["seq"])
        await self.bob.wait("control-writes", lambda item: not item["items"], since=mark)
        reader = self.burn_delivery(message_id, "bob")
        self.assertEqual(5000, reader["burn_at_ms"] - reader["burn_started_at_ms"])
        mark = await second.command("receipt", conversation=self.conversation, seq=message["seq"])
        await second.wait("control-writes", lambda item: not item["items"], since=mark)
        self.assertEqual(reader, self.burn_delivery(message_id, "bob"))
        await self.check_sync()
        self.marks = [len(client.events) for client in self.clients]
        await self.server.close(kill=True)
        self.assertNotEqual(0, self.server.process.returncode)
        await self.bob.crash()
        stopped_at = int(time.time() * 1000)
        self.assertLess(stopped_at, reader["burn_at_ms"])
        self.assertNotEqual(0, self.bob.process.returncode)
        (self.output / "bob-before-crash.json").write_text(json.dumps(self.bob.events), encoding="utf-8")
        (self.output / "bob-crash.json").write_text(json.dumps(dict(pid=self.bob.process.pid,
            exitCode=self.bob.process.returncode, stoppedAt=stopped_at, deadline=reader["burn_at_ms"])), encoding="utf-8")
        # Let real wall time cross the persisted deadline while the service is absent.
        await self.until(lambda: int(time.time() * 1000) > reader["burn_at_ms"] + 100, timeout=8)
        self.assertIsNone(self.burn_delivery(message_id, "bob")["burned_at_ms"])
        await self.start_server()
        replacement = await NativeClient.start(self.driver_path, self.output / "bob-restored.log", self.root / "state-bob")
        self.clients[1] = self.bob = replacement
        await replacement.connect(self.endpoint, "bob")
        await self.until(lambda: self.scalar("SELECT content_purged_at_ms FROM messages") > 0)
        for user in ("alice", "bob", "bob-second"):
            await self.until(lambda user=user: self.cached_message(user, message_id)["recalled"])
            self.assertEqual("", self.cached_message(user, message_id)["text"])
        await self.check_sync()
        reader_after = self.burn_delivery(message_id, "bob")
        self.assertEqual(reader["burn_at_ms"], reader_after["burn_at_ms"])
        self.assertEqual(reader["burn_started_at_ms"], reader_after["burn_started_at_ms"])
        self.assertGreaterEqual(reader_after["burned_at_ms"], reader["burn_at_ms"])
        self.assertEqual(b"", self.scalar("SELECT content FROM messages"))
        self.assertEqual(1, self.scalar("SELECT recalled FROM messages"))
        self.assert_burn_copies(message_id, "alice", True)
        self.assert_burn_copies(message_id, "bob", True)
        recalls = self.rows("SELECT event_id,user_id,seq,payload FROM sync_events WHERE event_type='recall' ORDER BY user_id")
        self.assertEqual(2, len(recalls))
        deliveries = self.rows("SELECT * FROM message_deliveries ORDER BY user_id")
        # A fresh cache must replay redacted history; repeated scans must keep event identity and time.
        fresh = await NativeClient.start(self.driver_path, self.output / "bob-fresh.log", self.root / "state-bob-fresh")
        self.clients.append(fresh)
        await fresh.connect(self.endpoint, "bob", "device-bob-fresh")
        await self.until(lambda: self.cached_message("bob-fresh", message_id) is not None and
                         self.cached_message("bob-fresh", message_id)["recalled"])
        self.assertEqual("", self.cached_message("bob-fresh", message_id)["text"])
        self.assertFalse(any(packet["event"] == "message" and packet["data"].get("text") == "private across restart"
                             for packet in fresh.events))
        await self.check_burn_device_sync("bob-second")
        await self.check_burn_device_sync("bob-fresh")
        await self.server.wait("burn-scan", lambda item: item["events"] == 0, since=len(self.server.events))
        self.assertEqual(recalls, self.rows("SELECT event_id,user_id,seq,payload FROM sync_events WHERE event_type='recall' ORDER BY user_id"))
        self.assertEqual(deliveries, self.rows("SELECT * FROM message_deliveries ORDER BY user_id"))

    async def test_burn_scan_rolls_back_before_server_commit(self):
        await self.burn_crash("before-commit")

    async def test_burn_scan_commit_survives_lost_push(self):
        await self.burn_crash("after-commit")

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
