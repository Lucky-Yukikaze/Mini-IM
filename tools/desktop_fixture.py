"""Launch a real Qt desktop with isolated QUIC services and inspectable test data.

Read context.json for endpoint, seeded group and local WebEngine debugging URL.
Write {"id": "unique-command", "op": ...} to command.json and wait for the same
id in response.json. Operations: snapshot, message(text), reject(code),
drop-ack(count), cache-fault(enabled,user,device,table), file-init-reject(code),
file-cancel-reject(code), confirm-bob, read-bob, read-cindy, restart-client, stop.
File checks also use pause-upload(offset), pause-download(offset),
prepare-download-target and corrupt-download-source(fileId,enabled).
Only the private fixture databases are changed. No default development data is used.
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
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import StreamDataReceived
from protocol.pb import common_pb2, conversation_pb2, message_pb2, sync_pb2
from quic.endpoint import serve_quic
from quic.download import STREAM_BUFFER_LIMIT
from quic.server import FaultConfig, MiniImQuicProtocol, OnlineSessionHub, ensure_dev_cert
from services.auth.service import AuthService
from services.control.service import ControlWriteService
from services.conversation.service import ConversationService
from services.delivery.service import DeliveryService
from services.file.service import FileService, FileServiceResult
from services.message.service import MessageService
from services.sync.service import SyncService
from storage.repo import ConversationRepo, DeliveryRepo, FileRepo, MessageRepo, SyncRepo
from storage.repo.control_write_repo import ControlWriteRepo, ControlWriteResult
from storage.sqlite.db import MiniImSqliteDb


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    deadline = time.monotonic() + 2
    while True:
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


class DesktopFixture:
    def __init__(self, args, root, output):
        self.args, self.root, self.output = args, root, output
        self.db = MiniImSqliteDb(root / "server.db")
        self.db.init_schema()
        self.server = self.client = self.client_log = None
        self.protocols = []
        self.attempts = []
        self.rejected = 0
        self.drop_acks = 0
        self.file_init_reject = self.file_cancel_reject = 0
        self.file_cancel_attempts = []
        self.file_attempts = []
        self.client_exits = []
        self.pause_upload_at = self.pause_download_at = 0
        self.held_upload_chunks = []
        self.context = {}
        self.hub = OnlineSessionHub()
        self.repo = ConversationRepo(self.db)
        self.conversations = ConversationService(self.repo)
        self.messages = MessageService(MessageRepo(self.db), self.repo)
        self.server_log = (output / "server.log").open("w", encoding="utf-8")

    async def start(self):
        for user in ("alice", "bob", "cindy"):
            self.conversations.ensure_user(user)
        group = self.conversations.handle_create_conversation("alice", "setup", conversation_pb2.CreateConversation(
            client_conv_id="desktop-group", title="Desktop QA", type=common_pb2.CONVERSATION_GROUP, member_ids=["bob"]))
        self.group = group.ack.entity_id
        scenario = self
        delivery = DeliveryService(DeliveryRepo(self.db))

        class Controls(ControlWriteService):
            def handle(self, user_id, envelope):
                operation = envelope.WhichOneof("body")
                scenario.attempts.append(dict(user=user_id, requestId=envelope.request_id, operation=operation,
                    body=getattr(envelope, operation).SerializeToString().hex()))
                if scenario.rejected:
                    return ControlWriteResult(message_pb2.Ack(request_id=envelope.request_id, success=False,
                        code=scenario.rejected, message="injected desktop control failure"), [])
                return super().handle(user_id, envelope)

        controls = Controls(ControlWriteRepo(self.db), self.conversations, delivery)
        class Files(FileService):
            def handle_file_init(self, user_id, request_id, file_init):
                if scenario.file_init_reject:
                    return FileServiceResult(message_pb2.Ack(request_id=request_id, success=False,
                        code=scenario.file_init_reject, message="injected file init rejection"), None, [])
                result = super().handle_file_init(user_id, request_id, file_init)
                scenario.file_attempts.append(dict(user=user_id, requestId=request_id,
                    intent=file_init.client_file_id, direction=file_init.direction, offset=file_init.resume_offset,
                    source=file_init.source_file_id, fileId=result.ack.entity_id, success=result.ack.success,
                    acceptedOffset=result.file_updated.transferred_bytes if result.file_updated else None))
                return result

            def handle_file_cancel(self, user_id, request_id, request):
                scenario.file_cancel_attempts.append(dict(user=user_id, requestId=request_id,
                    intent=request.client_file_id, body=request.SerializeToString().hex()))
                if scenario.file_cancel_reject:
                    return FileServiceResult(message_pb2.Ack(request_id=request_id, success=False,
                        code=scenario.file_cancel_reject, message="injected file cancel rejection"), None, [])
                return super().handle_file_cancel(user_id, request_id, request)

        files = self.files = Files(FileRepo(self.db), self.repo, MessageRepo(self.db), self.root / "files", 900000)
        auth = AuthService()

        class Protocol(MiniImQuicProtocol):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                scenario.protocols.append(self)
                original_pending = self.m_download_sender.buffer.pending_bytes
                def paused_pending(stream_id):
                    job = self.m_download_sender.jobs.get(stream_id)
                    if scenario.pause_download_at and job and job.offset >= scenario.pause_download_at:
                        return STREAM_BUFFER_LIMIT
                    return original_pending(stream_id)
                self.m_download_sender.buffer.pending_bytes = paused_pending

            def _handle_file_stream_data(self, stream_id, data, end_stream):
                state = self.m_file_stream_states.get(stream_id)
                if scenario.pause_upload_at and state and state.lease and state.lease.offset >= scenario.pause_upload_at:
                    scenario.held_upload_chunks.append((self, stream_id, data, end_stream))
                    return
                super()._handle_file_stream_data(stream_id, data, end_stream)

            def _send(self, stream_id, envelope):
                if envelope.HasField("ack") and "-control-" in envelope.request_id and scenario.drop_acks:
                    scenario.drop_acks -= 1
                    return
                super()._send(stream_id, envelope)

            def _debug(self, message):
                scenario.server_log.write(message + "\n")
                scenario.server_log.flush()

        cert, key = self.root / "cert.pem", self.root / "key.pem"
        ensure_dev_cert(cert, key)
        config = QuicConfiguration(is_client=False, alpn_protocols=["mini-im"])
        config.load_cert_chain(str(cert), str(key))
        self.server = await serve_quic("127.0.0.1", 0, configuration=config,
            create_protocol=lambda *args, **kwargs: Protocol(*args, auth_service=auth,
                conversation_service=self.conversations, control_write_service=controls, file_service=files,
                message_service=self.messages, sync_service=SyncService(SyncRepo(self.db)),
                online_hub=self.hub, fault_config=FaultConfig(), **kwargs))
        self.port = self.server._transport.get_extra_info("sockname")[1]
        with closing(socket.socket()) as probe:
            probe.bind(("127.0.0.1", 0))
            debug_port = probe.getsockname()[1]
        upload = self.root / "desktop-upload.bin"
        upload.write_bytes(b"desktop file cancellation" * 4096)
        transfer_dir = self.root / "desktop files"
        transfer_dir.mkdir()
        transfer_source = transfer_dir / "source.bin"
        transfer_source.write_bytes(bytes(range(251)) * 8192)
        transfer_target = transfer_dir / "download.bin"
        transfer_target.write_bytes(b"keep original destination until verified")
        self.context = dict(transferSource=str(transfer_source), transferTarget=str(transfer_target), upload=str(upload), endpoint=f"quic://127.0.0.1:{self.port}", group=self.group,
            debug=f"http://127.0.0.1:{debug_port}", root=str(self.root), output=str(self.output),
            state=str(self.root / "state"), users=["alice", "bob", "cindy"], device="desktop-device")
        await self.start_client()

    async def start_client(self):
        env = dict(os.environ, MINIIM_DEBUG_LOG="0", MINIIM_STATE_ROOT=self.context["state"],
            QTWEBENGINE_REMOTE_DEBUGGING=self.context["debug"].removeprefix("http://"), QT_QPA_PLATFORM="offscreen",
            QT_QPA_PLATFORM_PLUGIN_PATH=str(self.args.qt_root / "plugins/platforms"))
        env.pop("MINIIM_WEB_URL", None)
        env.pop("MINIIM_WEB_SMOKE_TEST", None)
        self.client_log = (self.output / "qt.log").open("ab")
        self.client = await asyncio.create_subprocess_exec(str(self.args.client), env=env,
            stdout=self.client_log, stderr=self.client_log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.context["pid"] = self.client.pid
        write_json(self.output / "context.json", self.context)

    async def stop_client(self):
        if self.client and self.client.returncode is None:
            self.client.terminate()
            await self.client.wait()
            self.client_exits.append(dict(pid=self.client.pid, exitCode=self.client.returncode))
        self.held_upload_chunks.clear()
        if self.client_log:
            self.client_log.close()

    def snapshot(self):
        artifacts, artifact_errors = {}, {}
        for path in (self.root / "desktop files").glob("*"):
            if path.is_file():
                try:
                    content = path.read_bytes()
                except (FileNotFoundError, PermissionError) as error:
                    # Qt may hold or rename its publication file during a snapshot.
                    artifact_errors[path.name] = type(error).__name__
                    continue
                artifacts[path.name] = dict(size=len(content), sha256=hashlib.sha256(content).hexdigest())
        state = dict(attempts=self.attempts, fileCancelAttempts=self.file_cancel_attempts,
            fileAttempts=self.file_attempts, clientExits=self.client_exits, artifacts=artifacts, artifactErrors=artifact_errors,
            transfers=[dict(row) for row in self.db.execute_fetchall("SELECT * FROM file_transfers")],
            fileTasks={},
            cancellations=[dict(row) for row in self.db.execute_fetchall("SELECT * FROM file_cancellations")],
            conversations=[dict(row) for row in self.db.execute_fetchall("SELECT * FROM conversations")],
            members=[dict(row) for row in self.db.execute_fetchall("SELECT * FROM conversation_members")],
            events=self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0],
            messages=[dict(row) for row in self.db.execute_fetchall(
                "SELECT server_msg_id,conversation_id,sender_id,conversation_seq,recalled FROM messages")], caches={})
        for path in (self.root / "state").glob("*.sqlite"):
            with closing(sqlite3.connect(path)) as cache:
                cache.row_factory = sqlite3.Row
                state["caches"][path.name] = [dict(row) for row in cache.execute(
                    "SELECT request_id,operation,status,code,error,attempts FROM control_outbox")]
                state["fileTasks"][path.name] = [dict(row) for row in cache.execute(
                    "SELECT id,init_request,finish_request,file_id,status FROM file_tasks")]
        return state

    async def command(self, data):
        op = data["op"]
        if op == "reject":
            self.rejected = int(data["code"])
        elif op == "file-init-reject":
            self.file_init_reject = int(data["code"])
        elif op == "file-cancel-reject":
            self.file_cancel_reject = int(data["code"])
        elif op == "drop-ack":
            self.drop_acks = int(data.get("count", 1))
        elif op == "message":
            result = self.messages.handle_send_message("bob", data["id"], message_pb2.SendMessage(
                conversation_id=self.group, client_msg_id=data["id"], type=common_pb2.MSG_TEXT,
                content=data["text"].encode("utf-8")))
            if not result.ack.success:
                raise ValueError(result.ack.message)
            self.hub.fanout_sync_events(result.sync_events)
        elif op == "confirm-bob":
            cursor = self.db.execute_fetchone("SELECT MAX(seq) FROM sync_events WHERE user_id='bob'")[0]
            result = SyncService(SyncRepo(self.db)).handle_sync_applied(
                "bob", "desktop-fixture-bob", data["id"], sync_pb2.SyncApplied(global_cursor=cursor))
            if not result.ack.success:
                raise ValueError(result.ack.message)
            self.hub.fanout_sync_events(result.sync_events)
        elif op in ("read-bob", "read-cindy"):
            seq = self.db.execute_fetchone("SELECT MAX(conversation_seq) FROM messages WHERE conversation_id=?", (self.group,))[0]
            result = DeliveryRepo(self.db).apply_receipt("bob" if op == "read-bob" else "cindy", self.group, seq)
            self.hub.fanout_sync_events(result.sync_events)
        elif op == "prepare-download-target":
            Path(self.context["transferTarget"]).write_bytes(b"keep original destination until verified")
        elif op == "corrupt-download-source":
            transfer = self.files.get_transfer_by_file_id(data["fileId"])
            if transfer is None or transfer.conversation_id != self.group or transfer.direction != 1:
                raise ValueError("fixture upload required")
            source = Path(self.context["transferSource"]).read_bytes()
            if data["enabled"]:
                source = bytes([source[0] ^ 255]) + source[1:]
            self.files.get_storage_path(data["fileId"]).write_bytes(source)
        elif op == "pause-upload":
            self.pause_upload_at = int(data["offset"])
            if not self.pause_upload_at:
                pending, self.held_upload_chunks = self.held_upload_chunks, []
                for protocol, stream, chunk, ended in pending:
                    # Re-enter the real queue, preserving FIN bookkeeping and yielding between chunks.
                    protocol.quic_event_received(StreamDataReceived(data=chunk, end_stream=ended, stream_id=stream))
        elif op == "pause-download":
            self.pause_download_at = int(data["offset"])
            for protocol in self.protocols:
                protocol.m_download_sender.notify()
        elif op == "cache-fault":
            identity = json.dumps([f"127.0.0.1:{self.port}", data.get("user", "alice"),
                data.get("device", "desktop-device")], separators=(",", ":"), ensure_ascii=False)
            path = self.root / "state" / (hashlib.sha256(identity.encode()).hexdigest() + ".sqlite")
            if not path.is_file():
                raise ValueError("connect the selected fixture account before injecting its cache fault")
            with closing(sqlite3.connect(path)) as cache, cache:
                table = data.get("table", "control_outbox")
                if table not in ("control_outbox", "file_tasks"):
                    raise ValueError("unsupported fixture cache table")
                if data["enabled"]:
                    cache.execute(f"CREATE TRIGGER qa_reject_control BEFORE INSERT ON {table} "
                        "BEGIN SELECT RAISE(ABORT, 'desktop injected save failure'); END")
                else:
                    cache.execute("DROP TRIGGER qa_reject_control")
        elif op == "restart-client":
            await self.stop_client()
            await self.start_client()
        elif op not in ("snapshot", "stop"):
            raise ValueError("unknown fixture operation")
        return self.snapshot()

    async def close(self):
        await self.stop_client()
        if self.server:
            await self.hub.writes.stop()
            self.server.close()
        for protocol in self.protocols:
            protocol.m_download_sender.close()
            await protocol.m_download_sender.wait_closed()
        write_json(self.output / "final-state.json", self.snapshot())
        self.db.close()
        self.server_log.close()


async def run(args):
    output = args.output.resolve() / time.strftime("%Y%m%d-%H%M%S")
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="fixture-", dir=output) as temporary:
        fixture = DesktopFixture(args, Path(temporary), output)
        try:
            await fixture.start()
            print(json.dumps(fixture.context), flush=True)
            seen = None
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                try:
                    data = json.loads((output / "command.json").read_text(encoding="utf-8-sig"))
                except (FileNotFoundError, PermissionError, json.JSONDecodeError):
                    await asyncio.sleep(0.1)
                    continue
                if data["id"] != seen:
                    seen = data["id"]
                    try:
                        state = await fixture.command(data)
                        write_json(output / "response.json", dict(id=seen, ok=True, state=state))
                    except Exception as error:
                        write_json(output / "response.json", dict(id=seen, ok=False, error=str(error)))
                    if data["op"] == "stop":
                        break
                await asyncio.sleep(0.1)
        finally:
            await fixture.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", type=Path, default=ROOT / "build/client-manifest/Release/mini_im_client.exe")
    parser.add_argument("--qt-root", type=Path, required=True, help="Qt kit directory containing plugins/platforms")
    parser.add_argument("--output", type=Path, default=ROOT / "tmp/desktop-integration")
    parser.add_argument("--timeout", type=int, default=900, help="maximum fixture lifetime in seconds")
    args = parser.parse_args()
    if not args.client.is_file() or not (args.qt_root / "plugins/platforms").is_dir() or args.timeout <= 0:
        parser.error("provide a built desktop, installed Qt kit and positive timeout")
    args.client = args.client.resolve()
    asyncio.run(run(args))
