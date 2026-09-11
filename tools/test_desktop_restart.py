"""Kill a real Qt desktop and an independent production server at durable boundaries.

Requires the built desktop client, Qt kit and existing Playwright CLI. Every
case has an isolated database, certificate, cache, file tree and loopback port.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import time
import unittest

from desktop_fixture import write_json
from test_desktop_ui import DesktopCheck
from test_server_restart import ServerProcess
from protocol.pb import common_pb2, conversation_pb2, file_pb2, message_pb2
from services.conversation.service import ConversationService
from storage.repo import ConversationRepo
from storage.sqlite.db import MiniImSqliteDb
from storage.sqlite.init_db import init_db

ROOT = Path(__file__).resolve().parents[1]


class DesktopRestartTest(unittest.IsolatedAsyncioTestCase):
    args: argparse.Namespace
    output_dir: Path

    async def asyncSetUp(self):
        self.output = self.output_dir / self._testMethodName
        self.output.mkdir()
        self.temporary = tempfile.TemporaryDirectory(prefix="fixture-", dir=self.output)
        self.root = Path(self.temporary.name)
        self.data = self.root / "server"
        self.db_path = self.data / "storage/sqlite/miniim.db"
        self.db_path.parent.mkdir(parents=True)
        self.servers, self.desktop_exits, self.crashes = [], [], []
        self.desktop = self.desktop_log = self.check = None
        self.addAsyncCleanup(self.cleanup)
        init_db(self.db_path)
        db = MiniImSqliteDb(self.db_path)
        try:
            service = ConversationService(ConversationRepo(db))
            for user in ("alice", "bob"):
                service.ensure_user(user)
            self.group = service.handle_create_conversation("alice", "setup", conversation_pb2.CreateConversation(
                client_conv_id="desktop-crash-group", type=common_pb2.CONVERSATION_GROUP,
                title="Desktop QA", member_ids=["bob"])).ack.entity_id
        finally:
            db.close()
        await self.start_server()
        with closing(socket.socket()) as probe:
            probe.bind(("127.0.0.1", 0))
            debug_port = probe.getsockname()[1]
        self.source, self.target = self.root / "source.bin", self.root / "download.bin"
        self.source.write_bytes(bytes(range(251)) * 8192)
        self.target.write_bytes(b"keep destination until verified")
        self.context = dict(endpoint=f"quic://127.0.0.1:{self.server.port}", group=self.group,
            root=str(self.root), output=str(self.output), state=str(self.root / "state"),
            debug=f"http://127.0.0.1:{debug_port}", device="desktop-crash-device",
            transferSource=str(self.source), transferTarget=str(self.target))
        await self.start_desktop()
        self.check = DesktopCheck(self.output / "context.json", self.args.playwright_cli)
        self.check.artifacts = ROOT / "output/playwright" / self.output_dir.name / self._testMethodName
        self.check.artifacts.mkdir(parents=True)
        await asyncio.to_thread(self.check.attach_client)
        await self.ui("login", user="alice", title="Desktop QA")
        await self.until(lambda: self.synced("alice"))

    async def start_server(self):
        port = self.servers[0].port if self.servers else 0
        self.server = await ServerProcess.start(self.data, port, self.output, len(self.servers), heartbeat=1)
        self.servers.append(self.server)
        if port:
            self.assertEqual(port, self.server.port)
            self.assertNotEqual(self.servers[-2].pid, self.server.pid)

    async def start_desktop(self):
        env = {key: value for key, value in os.environ.items() if not key.startswith("MINIIM_")}
        env.update(MINIIM_DEBUG_LOG="0", MINIIM_STATE_ROOT=self.context["state"],
            QTWEBENGINE_REMOTE_DEBUGGING=self.context["debug"].removeprefix("http://"),
            QT_QPA_PLATFORM="offscreen", QT_QPA_PLATFORM_PLUGIN_PATH=str(self.args.qt_root / "plugins/platforms"))
        self.desktop_log = (self.output / "qt.log").open("ab")
        self.desktop = await asyncio.create_subprocess_exec(str(self.args.client), env=env,
            stdout=self.desktop_log, stderr=self.desktop_log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.context["pid"] = self.desktop.pid
        write_json(self.output / "context.json", self.context)

    async def stop_desktop(self):
        if self.desktop is not None and self.desktop.returncode is None:
            self.desktop.kill()
            await asyncio.wait_for(self.desktop.wait(), 10)
            self.desktop_exits.append(dict(pid=self.desktop.pid, exitCode=self.desktop.returncode))
        if self.desktop_log:
            self.desktop_log.close()

    async def ui(self, phase, **fields):
        if phase == "crash-recovered":
            fields["image"] = str(self.check.artifacts / "recovered.png")
        await asyncio.to_thread(self.check.phase, phase, **fields)
        if phase == "login":
            self.active_user = fields["user"]
        if phase == "crash-recovered":
            self.final_evidence()

    def final_evidence(self):
        self.assertEqual("ok", self.scalar("PRAGMA integrity_check"))
        self.assertEqual([], self.rows("PRAGMA foreign_key_check"))
        self.assertTrue(self.synced(self.active_user))
        cursor = int(self.scalar("SELECT value FROM metadata WHERE key='cursor'", path=self.cache(self.active_user)))
        self.assertGreaterEqual(cursor, self.crashes[-1]["cursor"])
        summaries = {}
        for table, terminal in (("message_outbox", ("confirmed",)), ("control_outbox", ("confirmed",)),
                                ("file_tasks", ("completed", "cancelled"))):
            summaries[table] = self.rows(f"SELECT status,COUNT(*) AS count FROM {table} GROUP BY status", path=self.cache(self.active_user))
            self.assertTrue(all(row["status"] in terminal for row in summaries[table]))
        artifacts = {}
        for path in [self.source, self.target, *sorted((self.data / "storage/files").rglob("*"))]:
            if path.is_file():
                content = path.read_bytes()
                artifacts[str(path.relative_to(self.root))] = dict(size=len(content), sha256=hashlib.sha256(content).hexdigest())
        write_json(self.output / "final-state.json", dict(activeUser=self.active_user, cache=self.cache(self.active_user).name,
            integrity="ok", foreignKeyErrors=0, terminalTasks=summaries, artifacts=artifacts,
            transfers=self.rows("SELECT file_id,client_file_id,status,received_bytes,file_size FROM file_transfers"),
            messages=self.scalar("SELECT COUNT(*) FROM messages"), screenshot=str(self.check.artifacts / "recovered.png")))

    def cache(self, user):
        identity = json.dumps([self.context["endpoint"].removeprefix("quic://"), user,
            self.context["device"]], separators=(",", ":"))
        return Path(self.context["state"]) / (hashlib.sha256(identity.encode()).hexdigest() + ".sqlite")

    def rows(self, query, params=(), path=None):
        target = Path(path or self.db_path)
        try:
            with closing(sqlite3.connect(target, timeout=3)) as db:
                db.row_factory = sqlite3.Row
                return [dict(row) for row in db.execute(query, params)]
        except sqlite3.Error as error:
            write_json(self.output / "sqlite-error.json", dict(error=str(error),
                code=getattr(error, "sqlite_errorcode", None), name=getattr(error, "sqlite_errorname", None),
                path=str(target), query=query))
            raise

    def scalar(self, query, params=(), path=None):
        return next(iter(self.rows(query, params, path)[0].values()))

    async def until(self, predicate, timeout=30):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.05)

    def synced(self, user):
        metadata = {row["key"]: row["value"] for row in self.rows("SELECT key,value FROM metadata", path=self.cache(user))}
        expected = self.rows("SELECT seq,event_id FROM sync_events WHERE user_id=? ORDER BY seq", (user,))
        if int(metadata.get("sync_confirmed_cursor", 0)) != len(expected) or metadata.get("sync_confirmation"):
            return False
        self.assertEqual(int(metadata["cursor"]), len(expected))
        self.assertEqual(expected, self.rows("SELECT position AS seq,event_id FROM seen ORDER BY position", path=self.cache(user)))
        return True

    def backup(self, source, target):
        with closing(sqlite3.connect(source)) as db, closing(sqlite3.connect(self.output / target)) as destination:
            db.backup(destination)

    async def crash(self, user="alice"):
        checkpoint = await self.server.wait("checkpoint", timeout=25)
        desktop_pid = self.desktop.pid
        await asyncio.gather(self.server.close(kill=True), self.stop_desktop())
        self.assertEqual(checkpoint["pid"], self.server.pid)
        self.assertNotEqual(0, self.server.service_exit_code)
        self.assertNotEqual(0, self.desktop.returncode)
        self.backup(self.db_path, "server-before-restart.sqlite")
        self.backup(self.cache(user), "client-before-restart.sqlite")
        metadata = {row["key"]: row["value"] for row in self.rows("SELECT key,value FROM metadata", path=self.cache(user))}
        self.crashes.append(dict(checkpoint=checkpoint, serverPid=self.server.pid,
            serverExitCode=self.server.service_exit_code, desktopPid=desktop_pid, desktopExitCode=self.desktop.returncode,
            cursor=int(metadata.get("cursor", 0))))
        write_json(self.output / "joint-crashes.json", self.crashes)
        return checkpoint

    async def recover(self, user="alice", title="Desktop QA"):
        old_pid = self.desktop.pid
        await self.start_server()
        await self.start_desktop()
        self.assertNotEqual(old_pid, self.desktop.pid)
        self.crashes[-1].update(replacementDesktopPid=self.desktop.pid, replacementServerPid=self.server.pid)
        await asyncio.to_thread(self.check.call, "detach")
        await asyncio.to_thread(self.check.attach_client)
        await self.ui("login", user=user, title=title)

    async def replayed(self, operation, request_id, same_body=True):
        await self.server.wait("request", lambda event: event["operation"] == operation and event["requestId"] == request_id, timeout=20)
        requests = [event for server in self.servers for event in server.events
            if event["event"] == "request" and event["operation"] == operation and event["requestId"] == request_id]
        self.assertEqual(2, len({event["pid"] for event in requests}))
        if same_body:
            self.assertEqual(1, len({event["body"] for event in requests}))

    async def message_case(self, point):
        await self.server.arm(point, "send_message")
        await self.ui("crash-submit-message", text="desktop joint message")
        checkpoint = await self.crash()
        request = checkpoint["requestId"]
        before = self.rows("SELECT * FROM message_outbox WHERE request_id=?", (request,), self.cache("alice"))[0]
        self.assertNotEqual("confirmed", before["status"])
        self.assertEqual(int(point == "before-ack"), self.scalar("SELECT COUNT(*) FROM messages"))
        await self.recover()
        await self.replayed("send_message", request)
        await self.until(lambda: self.scalar("SELECT status FROM message_outbox WHERE request_id=?", (request,), self.cache("alice")) == "confirmed")
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM messages WHERE client_msg_id=?", (before["client_msg_id"],)))
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM control_write_results WHERE request_id=?", (request,)))
        await self.until(lambda: self.synced("alice"))
        await self.ui("crash-recovered", title="Desktop QA", text="desktop joint message")

    async def rename_case(self, point):
        await self.server.arm(point, "rename_conversation")
        await self.ui("crash-submit-rename", title="Desktop renamed")
        checkpoint = await self.crash()
        request = checkpoint["requestId"]
        self.assertNotEqual("confirmed", self.scalar("SELECT status FROM control_outbox WHERE request_id=?", (request,), self.cache("alice")))
        self.assertEqual("Desktop renamed" if point == "before-ack" else "Desktop QA", self.scalar("SELECT title FROM conversations"))
        await self.recover(title="Desktop renamed")
        await self.replayed("rename_conversation", request)
        await self.until(lambda: self.scalar("SELECT status FROM control_outbox WHERE request_id=?", (request,), self.cache("alice")) == "confirmed")
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM control_write_results WHERE request_id=?", (request,)))
        await self.until(lambda: self.synced("alice"))
        await self.ui("crash-recovered", title="Desktop renamed")

    async def upload_case(self, point, operation, position=0):
        await self.server.arm(point, operation, position)
        await self.ui("file-upload")
        checkpoint = await self.crash()
        task = self.rows("SELECT * FROM file_tasks", path=self.cache("alice"))[0]
        self.assertNotIn(task["status"], ("completed", "cancelled"))
        transfer = self.rows("SELECT * FROM file_transfers")[0]
        committed_events = self.rows("SELECT user_id,seq,event_id,payload FROM sync_events WHERE event_type='file_updated' ORDER BY user_id,seq")
        committed_result = None
        if point == "before-ack":
            committed_result = self.scalar("SELECT ack FROM control_write_results WHERE request_id=?", (checkpoint["requestId"],))
            acknowledgement = message_pb2.Ack.FromString(committed_result)
            self.assertTrue(acknowledgement.success)
            self.assertEqual(transfer["file_id"], acknowledgement.entity_id)
            self.assertEqual(task["finish_request"], checkpoint["requestId"])
            finished = [row for row in committed_events if row["user_id"] == "alice"
                and file_pb2.FileUpdated.FromString(row["payload"]).completed]
            self.assertEqual(1, len(finished))
        path = self.data / "storage/files" / transfer["storage_path"]
        if point == "file-flushed":
            self.assertGreater(path.stat().st_size, transfer["received_bytes"])
        elif point == "file-progress":
            self.assertEqual(path.stat().st_size, transfer["received_bytes"])
            self.assertGreater(transfer["received_bytes"], 0)
        else:
            self.assertEqual("completed" if point == "before-ack" else "uploaded", transfer["status"])
            self.assertEqual(int(point == "before-ack"), self.scalar("SELECT COUNT(*) FROM messages"))
        write_json(self.output / "file-checkpoint.json", dict(fileId=transfer["file_id"],
            committedBytes=transfer["received_bytes"], diskBytes=path.stat().st_size,
            intent=task["id"], initRequest=task["init_request"], finishRequest=task["finish_request"]))
        await self.recover()
        await self.until(lambda: self.scalar("SELECT status FROM file_tasks WHERE id=?", (task["id"],), self.cache("alice")) == "completed")
        if operation == "file_finish" and point == "before-commit":
            await self.replayed("file_finish", checkpoint["requestId"])
        elif point == "before-ack":
            # A persisted completed event can resolve an upload before retry is needed.
            self.assertEqual(committed_result, self.scalar("SELECT ack FROM control_write_results WHERE request_id=?", (checkpoint["requestId"],)))
            self.assertEqual(committed_events, self.rows("SELECT user_id,seq,event_id,payload FROM sync_events WHERE event_type='file_updated' ORDER BY user_id,seq"))
            await self.until(lambda: bool(self.rows("SELECT event_id FROM seen WHERE event_id=?", (finished[0]["event_id"],), self.cache("alice"))))
        else:
            await self.replayed("file_init", task["init_request"])
            initialized = next(event for event in self.server.events if event["event"] == "file-init" and event["requestId"] == task["init_request"])
            self.assertEqual(transfer["received_bytes"], initialized["offset"])
        after = self.rows("SELECT * FROM file_tasks WHERE id=?", (task["id"],), self.cache("alice"))[0]
        self.assertEqual((task["id"], task["init_request"], task["finish_request"]), (after["id"], after["init_request"], after["finish_request"]))
        self.assertEqual(self.source.read_bytes(), path.read_bytes())
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM messages"))
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM file_transfers"))
        await self.until(lambda: self.synced("alice"))
        self.assertEqual("ok", self.scalar("PRAGMA integrity_check"))
        self.assertEqual([], self.rows("PRAGMA foreign_key_check"))
        await self.ui("crash-recovered", title="Desktop QA", fileName="source.bin")

    async def test_message_before_commit(self):
        await self.message_case("before-commit")

    async def test_message_after_commit(self):
        await self.message_case("before-ack")

    async def test_rename_before_commit(self):
        await self.rename_case("before-commit")

    async def test_rename_after_commit(self):
        await self.rename_case("before-ack")

    async def test_upload_uncommitted_tail(self):
        await self.upload_case("file-flushed", "upload", 65536)

    async def test_upload_committed_progress(self):
        await self.upload_case("file-progress", "upload", 65536)

    async def test_file_finish_before_commit(self):
        await self.upload_case("before-commit", "file_finish")

    async def test_file_finish_after_commit(self):
        await self.upload_case("before-ack", "file_finish")

    async def test_partial_download(self):
        await self.ui("file-upload")
        await self.until(lambda: self.scalar("SELECT COUNT(*) FROM file_transfers WHERE status='completed'") == 1)
        await self.ui("file-complete")
        file_id = self.scalar("SELECT file_id FROM file_transfers")
        await self.ui("login", user="bob", title="Desktop QA", switch=True)
        await self.until(lambda: self.synced("bob"))
        await self.server.arm("download-read", "download", 262144)
        await self.ui("file-fill", fileId=file_id)
        await self.ui("file-download", fileId=file_id, pending=True)
        await self.until(lambda: any(path.stat().st_size >= 65536 for path in self.root.glob("download.bin.miniim-*.part")))
        checkpoint = await self.crash("bob")
        part = next(self.root.glob("download.bin.miniim-*.part"))
        partial = part.stat().st_size
        self.assertGreaterEqual(partial, 65536)
        self.assertLess(partial, self.source.stat().st_size)
        self.assertEqual(b"keep destination until verified", self.target.read_bytes())
        task = self.rows("SELECT * FROM file_tasks", path=self.cache("bob"))[0]
        await self.recover("bob")
        await self.until(lambda: self.scalar("SELECT status FROM file_tasks WHERE id=?", (task["id"],), self.cache("bob")) == "completed")
        await self.replayed("file_init", task["init_request"], same_body=False)
        initialized = next(event for event in self.server.events if event["event"] == "file-init" and event["requestId"] == task["init_request"])
        self.assertEqual(partial, initialized["requestedOffset"])
        after = self.rows("SELECT * FROM file_tasks", path=self.cache("bob"))[0]
        self.assertEqual((task["id"], task["init_request"], task["finish_request"]), (after["id"], after["init_request"], after["finish_request"]))
        self.assertEqual(self.source.read_bytes(), self.target.read_bytes())
        self.assertFalse(list(self.root.glob("download.bin.miniim-*.part")))
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM messages"))
        self.assertEqual(2, self.scalar("SELECT COUNT(*) FROM file_transfers"))
        write_json(self.output / "download-checkpoint.json", dict(partialBytes=partial, resumedOffset=initialized["requestedOffset"],
            sha256=hashlib.sha256(self.target.read_bytes()).hexdigest()))
        await self.until(lambda: self.synced("bob"))
        await self.ui("crash-recovered", title="Desktop QA", fileName="source.bin")

    async def cleanup(self):
        errors = []
        try:
            await self.stop_desktop()
            if self.check:
                await asyncio.to_thread(self.check.call, "detach")
        except Exception as error:
            errors.append(str(error))
        for server in self.servers:
            try:
                await server.close(kill=bool(errors) or not server.log.closed and any(event["event"] == "checkpoint" for event in server.events))
            except Exception as error:
                errors.append(str(error))
        write_json(self.output / "lifecycle.json", [dict(pid=server.pid, launcherPid=server.process.pid,
            exitCode=server.service_exit_code, launcherExitCode=server.process.returncode, port=server.port,
            hardKilled=server.hard_killed) for server in self.servers])
        write_json(self.output / "desktop-exits.json", self.desktop_exits)
        write_json(self.output / "joint-crashes.json", self.crashes)
        write_json(self.output / "ui-phases.json", self.check.results if self.check else [])
        if self.db_path.exists():
            self.backup(self.db_path, "final-server.sqlite")
        for cache in (self.root / "state").glob("*.sqlite"):
            self.backup(cache, "final-" + cache.name)
        self.temporary.cleanup()
        if errors:
            raise AssertionError(errors)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", type=Path, default=ROOT / "build/client-manifest/Release/mini_im_client.exe")
    parser.add_argument("--qt-root", type=Path, required=True)
    parser.add_argument("--playwright-cli", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "tmp/desktop-restart")
    parser.add_argument("--test", action="append")
    args = parser.parse_args()
    for path in (args.client, args.playwright_cli):
        if not path.is_file():
            parser.error(f"required file does not exist: {path}")
    output = args.output.resolve() / time.strftime("%Y%m%d-%H%M%S")
    output.mkdir(parents=True)
    DesktopRestartTest.args, DesktopRestartTest.output_dir = args, output
    names = args.test or unittest.defaultTestLoader.getTestCaseNames(DesktopRestartTest)
    for name in names:
        if not name.startswith("test_") or not callable(getattr(DesktopRestartTest, name, None)):
            parser.error("unknown test: " + name)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(DesktopRestartTest(name) for name in names))
    write_json(output / "results.json", dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
        successful=result.wasSuccessful(), scope="Real Qt WebEngine UI and independent server joint process termination"))
    print("Desktop restart evidence: " + str(output))
    raise SystemExit(not result.wasSuccessful())


if __name__ == "__main__":
    main()
