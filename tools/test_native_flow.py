"""Exercise the desktop bridge and MsQuic core against an isolated aioquic server.

Run with the same Python dependencies as the server and an already built native driver.
The driver command socket uses loopback; application traffic always uses real QUIC.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing, suppress
import hashlib
import json
import os
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from quic.endpoint import serve_quic
from aioquic.quic.configuration import QuicConfiguration

from protocol.codec import EnvelopeCodec
from protocol.pb import common_pb2, message_pb2
from quic.server import FaultConfig, MiniImQuicProtocol, OnlineSessionHub, ensure_dev_cert
from services.auth.service import AuthService
from services.conversation.service import ConversationService
from services.control.service import ControlWriteService
from services.delivery.service import DeliveryService
from services.file.service import FileService, FileServiceResult
from services.message.service import MessageService, SendMessageResult
from services.sync.service import SyncService
from storage.repo import ConversationRepo, DeliveryRepo, FileRepo, MessageRepo, SyncRepo
from storage.sqlite.db import MiniImSqliteDb
from storage.repo.control_write_repo import ControlWriteRepo, ControlWriteResult
from storage.sqlite.init_db import init_db


class NativeClient:
    def __init__(self, process, reader, writer, error_log):
        self.process = process
        self.reader = reader
        self.writer = writer
        self.error_log = error_log
        self.events = []
        self.condition = asyncio.Condition()
        self.closed = False
        self.cleaned_up = False
        self.read_task = asyncio.create_task(self._read())

    @classmethod
    async def start(cls, executable, log_path, state_root):
        error_log = log_path.open("wb")
        env = dict(os.environ, MINIIM_DEBUG_LOG="0", MINIIM_STATE_ROOT=str(state_root))
        try:
            process = await asyncio.create_subprocess_exec(
                str(executable), stdout=asyncio.subprocess.PIPE, stderr=error_log,
                env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            port_line = await asyncio.wait_for(process.stdout.readline(), 10)
            if not port_line.strip().isdigit():
                raise RuntimeError(f"native driver did not start: {port_line!r}; see {log_path}")
            reader, writer = await asyncio.open_connection("127.0.0.1", int(port_line))
            return cls(process, reader, writer, error_log)
        except BaseException:
            if "process" in locals() and process.returncode is None:
                process.kill()
                await process.wait()
            error_log.close()
            raise

    async def _read(self):
        try:
            while line := await self.reader.readline():
                async with self.condition:
                    self.events.append(json.loads(line))
                    self.condition.notify_all()
        finally:
            async with self.condition:
                self.closed = True
                self.condition.notify_all()

    async def wait(self, event, predicate=lambda data: True, since=0, timeout=15):
        try:
            async with asyncio.timeout(timeout):
                async with self.condition:
                    while True:
                        for packet in self.events[since:]:
                            if packet["event"] == event and predicate(packet["data"]):
                                return packet["data"]
                        if self.closed:
                            raise RuntimeError("native driver connection closed")
                        await self.condition.wait()
        except TimeoutError as exc:
            raise AssertionError(
                f"timed out waiting for {event}; recent events: {self.events[-12:]}"
            ) from exc

    async def command(self, operation, **data):
        mark = len(self.events)
        request_id = str(mark) + "-" + operation
        self.writer.write((json.dumps(dict(data, op=operation, id=request_id)) + "\n").encode())
        await self.writer.drain()
        result = await self.wait("result", lambda value: value["id"] == request_id, since=mark)
        if not result["accepted"]:
            raise AssertionError(f"native command rejected: {operation}; {self.events[mark:]}")
        return mark

    async def connect(self, endpoint, user, device=None):
        mark = await self.command("connect", endpoint=endpoint, token="dev-token:" + user, device=device or "device-" + user)
        return await self.wait("initial", lambda data: data["currentUser"]["userId"] == user, since=mark)

    async def crash(self):
        self.process.kill()
        await self.process.wait()
        await self.close(allow_crash=True)

    async def close(self, allow_crash=False):
        if self.cleaned_up:
            return
        self.cleaned_up = True
        self.writer.close()
        with suppress(ConnectionError):
            await self.writer.wait_closed()
        try:
            await asyncio.wait_for(self.process.wait(), 5)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()
        try:
            await self.read_task
        except ConnectionError:
            if not allow_crash:
                raise
        finally:
            self.error_log.close()
        if self.process.returncode != 0 and not allow_crash:
            raise AssertionError(f"native driver exited with {self.process.returncode}")


class TestProtocol(MiniImQuicProtocol):
    """Only fault timing differs from production; framing and transport are unchanged."""
    __test__ = False

    def __init__(self, *args, scenario, **kwargs):
        self.scenario = scenario
        super().__init__(*args, **kwargs)
        scenario.protocols.append(self)
        original_pending = self.m_download_sender.buffer.pending_bytes
        def paused_pending(stream_id):
            job = self.m_download_sender.jobs.get(stream_id)
            if scenario.pause_download_at and job and job.offset >= scenario.pause_download_at:
                return 131072
            return original_pending(stream_id)
        self.m_download_sender.buffer.pending_bytes = paused_pending

    def _send_error(self, stream_id, request, code, message):
        if code == 429 and message.startswith("upload is active"):
            self.scenario.upload_conflicts.append({"device": self.m_device_id, "requestId": request.request_id})
        super()._send_error(stream_id, request, code, message)

    def _handle_file_stream_data(self, stream_id, data, end_stream):
        state = self.m_file_stream_states.get(stream_id)
        if (self.scenario.hold_alice_upload and self.m_device_id == "device-alice"
                and state and state.lease and state.lease.offset >= 65536):
            self.scenario.held_upload_chunks.append((self, stream_id, data, end_stream))
            return
        super()._handle_file_stream_data(stream_id, data, end_stream)

    def _debug(self, message):
        self.scenario.server_log.write(message + "\n")
        self.scenario.server_log.flush()

    def send_sync_event(self, event):
        if self.m_user_id == "bob" and event.event_type == "message" and self.scenario.drop_next_bob_message:
            self.scenario.drop_next_bob_message = False
            return
        super().send_sync_event(event)

    def _send(self, stream_id, envelope):
        if (envelope.HasField("ack") and envelope.request_id in self.scenario.confirmation_requests
                and self.m_user_id == "bob" and self.scenario.drop_confirmation_acks):
            self.scenario.drop_confirmation_acks -= 1
            return
        if envelope.HasField("ack") and envelope.request_id in self.scenario.control_requests:
            if self.scenario.drop_control_acks:
                self.scenario.drop_control_acks -= 1
                return
            if self.scenario.duplicate_control_acks:
                super()._send(stream_id, envelope)
        if envelope.HasField("ack") and envelope.ack.success:
            key = "filecancel" if "-filecancel-" in envelope.request_id else "filefinish" if "-filefinish-" in envelope.request_id else "fileinit"
            if ("-filefinish-" in envelope.request_id or "-fileinit-" in envelope.request_id
                    or "-filedl-" in envelope.request_id or "-filecancel-" in envelope.request_id) and self.scenario.drop_file_acks.get(key, 0):
                self.scenario.drop_file_acks[key] -= 1
                return
        if self.scenario.silent_user and self.m_user_id == self.scenario.silent_user:
            return
        if envelope.HasField("welcome") and self.scenario.heartbeat_interval:
            envelope.welcome.heartbeat_interval_sec = self.scenario.heartbeat_interval
        if envelope.HasField("welcome") and self.scenario.drop_welcome_count:
            self.scenario.drop_welcome_count -= 1
            return
        if envelope.HasField("error") and envelope.error.code == 401:
            self.scenario.session_rejections.append(envelope.request_id)
        if envelope.HasField("ack") and "-msg-" in envelope.request_id:
            if self.scenario.drop_message_ack_count:
                self.scenario.drop_message_ack_count -= 1
                return
            super()._send(stream_id, envelope)
            if self.scenario.duplicate_message_acks:
                super()._send(stream_id, envelope)
            return
        if (self.m_user_id == "bob" and self.scenario.hold_bob_history
                and envelope.HasField("sync_response") and envelope.request_id):
            self.scenario.held_history_count += 1
            return
        if self.m_user_id == "bob" and envelope.HasField("sync_response") and envelope.request_id:
            empty = self.scenario.empty_bob_history_count > 0
            if empty:
                self.scenario.empty_bob_history_count -= 1
                envelope.sync_response.ClearField("events")
                envelope.sync_response.has_more = False
            self.scenario.sync_replies.append({
                "requestId": envelope.request_id, "empty": empty, "time": time.monotonic(),
                "positions": [event.global_seq for event in envelope.sync_response.events],
            })
        super()._send(stream_id, envelope)

    def _send_file_updated(self, stream_id, request, updated, sender_event):
        if self.scenario.delay_metadata and updated.direction == common_pb2.FILE_DIRECTION_DOWNLOAD:
            if self.scenario.hold_metadata:
                self.scenario.deferred_metadata.append(
                    lambda: super(TestProtocol, self)._send_file_updated(stream_id, request, updated, sender_event))
            else:
                asyncio.get_running_loop().call_later(
                    0.3, super()._send_file_updated, stream_id, request, updated, sender_event,
                )
        else:
            super()._send_file_updated(stream_id, request, updated, sender_event)


class RecordingMessageService(MessageService):
    def __init__(self, *args, scenario, **kwargs):
        super().__init__(*args, **kwargs)
        self.scenario = scenario

    def handle_send_message(self, user_id, request_id, send_message):
        self.scenario.message_attempts.append({
            "user": user_id, "requestId": request_id, "intent": send_message.client_msg_id,
            "body": send_message.SerializeToString().hex(),
        })
        rejected = self.scenario.rejected_message_intents.get(send_message.client_msg_id)
        if rejected:
            return SendMessageResult(
                ack=message_pb2.Ack(request_id=request_id, success=False, code=rejected, message="injected rejection"),
                message_push=None, sync_events=[],
            )
        return super().handle_send_message(user_id, request_id, send_message)


class RecordingControlService(ControlWriteService):
    def __init__(self, *args, scenario, **kwargs):
        super().__init__(*args, **kwargs)
        self.scenario = scenario

    def handle(self, user_id, envelope):
        operation = envelope.WhichOneof("body")
        self.scenario.control_requests.add(envelope.request_id)
        self.scenario.control_attempts.append({
            "user": user_id, "requestId": envelope.request_id, "operation": operation,
            "body": getattr(envelope, operation).SerializeToString().hex(),
        })
        if self.scenario.reject_control_code:
            return ControlWriteResult(message_pb2.Ack(
                request_id=envelope.request_id, code=self.scenario.reject_control_code,
                success=False, message="injected control write failure"), [])
        return super().handle(user_id, envelope)


class RecordingSyncService(SyncService):
    def __init__(self, repo, scenario):
        super().__init__(repo)
        self.scenario = scenario

    def handle_sync_applied(self, user_id, device_id, request_id, request):
        self.scenario.confirmation_requests.add(request_id)
        self.scenario.confirmation_attempts.append({"user": user_id, "device": device_id,
            "requestId": request_id, "cursor": request.global_cursor, "body": request.SerializeToString().hex()})
        if self.scenario.reject_confirmation and user_id == "bob":
            return ControlWriteResult(message_pb2.Ack(request_id=request_id, success=False,
                code=503, message="injected confirmation hold"), [])
        return super().handle_sync_applied(user_id, device_id, request_id, request)


class RecordingFileService(FileService):
    def __init__(self, *args, scenario, **kwargs):
        super().__init__(*args, **kwargs)
        self.scenario = scenario

    def handle_file_init(self, user_id, request_id, file_init):
        if self.scenario.block_file_init:
            return FileServiceResult(message_pb2.Ack(request_id=request_id, success=False, code=503,
                message="injected initialization hold"), None, [])
        result = super().handle_file_init(user_id, request_id, file_init)
        self.scenario.file_attempts.append({
            "user": user_id, "requestId": request_id, "intent": file_init.client_file_id,
            "offset": file_init.resume_offset, "fileId": result.ack.entity_id,
            "acceptedOffset": result.file_updated.transferred_bytes if result.file_updated else None,
        })
        return result


    def handle_file_cancel(self, user_id, request_id, file_cancel):
        self.scenario.cancel_attempts.append({"user": user_id, "requestId": request_id,
            "intent": file_cancel.client_file_id, "fileId": file_cancel.file_id,
            "body": file_cancel.SerializeToString().hex()})
        if self.scenario.reject_file_cancel:
            return FileServiceResult(message_pb2.Ack(request_id=request_id, success=False,
                code=self.scenario.reject_file_cancel, message="injected cancellation rejection"), None, [])
        return super().handle_file_cancel(user_id, request_id, file_cancel)

    def handle_file_finish(self, user_id, request_id, file_finish):
        if self.scenario.hold_upload_finish:
            return FileServiceResult(message_pb2.Ack(request_id=request_id, success=False, code=503,
                message="injected pending completion"), None, [])
        return super().handle_file_finish(user_id, request_id, file_finish)

    def append_file_chunk(self, user_id, file_id, chunk):
        fault = self.scenario.upload_storage_fault
        self.scenario.upload_storage_fault = ""
        if fault == "sync":
            with patch("os.fsync", side_effect=OSError("injected upload sync failure")):
                return super().append_file_chunk(user_id, file_id, chunk)
        if fault == "database":
            self.m_file_repo.m_db.execute_write("CREATE TRIGGER reject_upload_progress BEFORE UPDATE ON file_transfers "
                "BEGIN SELECT RAISE(ABORT, 'injected upload progress failure'); END")
            try:
                return super().append_file_chunk(user_id, file_id, chunk)
            finally:
                self.m_file_repo.m_db.execute_write("DROP TRIGGER reject_upload_progress")
        return super().append_file_chunk(user_id, file_id, chunk)


class NativeFlowTest(unittest.IsolatedAsyncioTestCase):
    driver_path: Path
    output_dir: Path

    async def asyncSetUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="fixture-", dir=self.output_dir)
        self.root = Path(self.workspace.name)
        self.server_log = (self.output_dir / (self._testMethodName + "-server.log")).open("w", encoding="utf-8")
        self.clients = []
        self.protocols = []
        self.server = None
        self.db = None
        self.addAsyncCleanup(self.cleanup)
        self.delay_metadata = False
        self.hold_metadata = False
        self.deferred_metadata = []
        self.drop_next_bob_message = False
        self.hold_bob_history = False
        self.held_history_count = 0
        self.drop_message_ack_count = 0
        self.duplicate_message_acks = False
        self.rejected_message_intents = {}
        self.message_attempts = []
        self.session_rejections = []
        self.drop_welcome_count = 0
        self.silent_user = ""
        self.heartbeat_interval = 0
        self.pause_download_at = 0
        self.drop_file_acks = {}
        self.file_attempts = []
        self.upload_storage_fault = ""
        self.hold_upload_finish = False
        self.hold_alice_upload = False
        self.held_upload_chunks = []
        self.upload_conflicts = []
        self.block_file_init = False
        self.reject_file_cancel = 0
        self.cancel_attempts = []
        self.confirmation_requests = set()
        self.confirmation_attempts = []
        self.drop_confirmation_acks = 0
        self.reject_confirmation = False
        self.control_requests = set()
        self.control_attempts = []
        self.drop_control_acks = 0
        self.duplicate_control_acks = False
        self.reject_control_code = 0
        self.fault = FaultConfig()
        self.db_path = self.root / "test.db"
        init_db(self.db_path)
        self.db = MiniImSqliteDb(self.db_path)
        conversations = ConversationRepo(self.db)
        messages = MessageRepo(self.db)
        deliveries = DeliveryRepo(self.db)
        self.files = RecordingFileService(FileRepo(self.db), conversations, messages, self.root / "files", 900000, scenario=self)
        self.empty_bob_history_count = 0
        self.sync_replies = []
        self.hub = OnlineSessionHub()
        cert, key = self.root / "cert.pem", self.root / "key.pem"
        ensure_dev_cert(cert, key)
        config = QuicConfiguration(is_client=False, alpn_protocols=["mini-im"])
        config.load_cert_chain(str(cert), str(key))
        auth = self.auth = AuthService()
        self.server = await serve_quic(
            "127.0.0.1", 0, configuration=config,
            create_protocol=lambda *args, **kwargs: TestProtocol(
                *args, scenario=self, auth_service=auth,
                conversation_service=ConversationService(conversations),
                control_write_service=RecordingControlService(
                    ControlWriteRepo(self.db), ConversationService(conversations), DeliveryService(deliveries), scenario=self),
                file_service=self.files, message_service=RecordingMessageService(messages, conversations, scenario=self),
                sync_service=RecordingSyncService(SyncRepo(self.db), self), online_hub=self.hub, fault_config=self.fault, **kwargs,
            ),
        )
        def record_udp_error(error):
            self.server_log.write(f"UDP listener error: {error!r}\n")
            self.server_log.flush()
        self.server.error_received = record_udp_error
        port = self.server._transport.get_extra_info("sockname")[1]
        self.endpoint = f"quic://127.0.0.1:{port}"
        for user in ("alice", "bob"):
            client = await NativeClient.start(
                self.driver_path, self.output_dir / f"{self._testMethodName}-{user}.log", self.root / ("state-" + user),
            )
            self.clients.append(client)
            await client.connect(self.endpoint, user)
        self.alice, self.bob = self.clients
        mark = await self.alice.command("direct", intent="direct-intent", peer="bob")
        conversation = await self.alice.wait("conversation", since=mark)
        self.conversation = conversation["conversationId"]
        await self.bob.wait("conversation", lambda item: item["conversationId"] == self.conversation)

    async def cleanup(self):
        errors = []
        (self.output_dir / f"{self._testMethodName}-confirmation-attempts.json").write_text(
            json.dumps(self.confirmation_attempts, indent=2), encoding="utf-8")
        (self.output_dir / f"{self._testMethodName}-upload-conflicts.json").write_text(
            json.dumps(self.upload_conflicts, indent=2), encoding="utf-8")
        (self.output_dir / f"{self._testMethodName}-control-attempts.json").write_text(
            json.dumps(self.control_attempts, indent=2), encoding="utf-8")
        (self.output_dir / f"{self._testMethodName}-sync-replies.json").write_text(
            json.dumps(self.sync_replies, indent=2), encoding="utf-8")
        (self.output_dir / f"{self._testMethodName}-cancel-attempts.json").write_text(
            json.dumps(self.cancel_attempts, indent=2), encoding="utf-8")
        (self.output_dir / f"{self._testMethodName}-file-attempts.json").write_text(
            json.dumps(self.file_attempts, indent=2), encoding="utf-8")

        (self.output_dir / f"{self._testMethodName}-message-attempts.json").write_text(
            json.dumps(self.message_attempts, indent=2), encoding="utf-8",
        )
        for index, client in enumerate(self.clients):
            (self.output_dir / f"{self._testMethodName}-client-{index}.json").write_text(
                json.dumps(client.events, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            try:
                await client.close()
            except Exception as exc:
                errors.append(exc)
        if self.server is not None:
            await self.hub.writes.stop()
            self.server.close()
        for protocol in self.protocols:
            protocol.m_download_sender.close()
            await protocol.m_download_sender.wait_closed()
        if self.db is not None:
            self.db.close()
        self.workspace.cleanup()
        self.server_log.close()
        if errors:
            raise errors[0]

    async def upload(self, payload):
        source = self.root / "source.bin"
        source.write_bytes(payload)
        mark = await self.alice.command("upload", conversation=self.conversation, path=str(source))
        complete = await self.alice.wait("file", lambda item: item["completed"], since=mark)
        file_id = complete["fileId"]
        for client in self.clients:
            await client.wait("message", lambda item: item["clientMsgId"] == "file-msg-" + file_id)
        return file_id


    async def test_multidevice_direct_reads_recall_and_restart(self):
        await self.check_multidevice_messages(group=False)

    async def test_multidevice_group_reads_recall_and_restart(self):
        await self.check_multidevice_messages(group=True)

    async def check_multidevice_messages(self, *, group):
        devices = [(self.alice, "alice", "device-alice", self.root / "state-alice"),
                   (self.bob, "bob", "device-bob", self.root / "state-bob")]
        for user in ("alice", "bob"):
            device = "device-" + user + "-secondary"
            state = self.root / ("state-" + user + "-secondary")
            client = await NativeClient.start(self.driver_path,
                self.output_dir / f"{self._testMethodName}-{device}.log", state)
            self.clients.append(client)
            await client.connect(self.endpoint, user, device=device)
            await self.synced(client, user)
            devices.append((client, user, device, state))
        alice_second, bob_second = devices[2][0], devices[3][0]
        conversation = self.conversation
        if group:
            mark = await self.alice.command("group", intent="multi-group", title="two devices each", members=["bob"])
            created = await self.alice.wait("conversation", lambda item: item["type"] == "group", since=mark)
            conversation = created["conversationId"]
            for client, _, _, _ in devices:
                await client.wait("conversation", lambda item: item["conversationId"] == conversation)

        await self.alice.command("message", conversation=conversation, intent="multi-read", text="read once")
        first = await self.bob.wait("message", lambda item: item["clientMsgId"] == "multi-read")
        for client, user, _, _ in devices:
            await client.wait("message", lambda item: item["id"] == first["id"])
            await self.synced(client, user)
        marks = [len(client.events) for client in (self.bob, bob_second)]
        await asyncio.gather(*(client.command("receipt", conversation=conversation, seq=first["seq"])
                              for client in (self.bob, bob_second)))
        for client, mark in zip((self.bob, bob_second), marks):
            await client.wait("control-writes", lambda item: not item["items"], since=mark)
        for client, user, _, _ in devices:
            await client.wait("update", lambda item: item["type"] == "receipt" and item["lastReadSeq"] == first["seq"])
            await self.synced(client, user)
        receipts = [item for item in self.control_attempts if item["operation"] == "receipt"]
        self.assertEqual(2, len({item["requestId"] for item in receipts}))
        for user in ("alice", "bob"):
            self.assertEqual(1, self.db.execute_fetchone(
                "SELECT COUNT(*) FROM sync_events WHERE user_id=? AND event_type='receipt'", (user,))[0])
        counter = self.db.execute_fetchone(
            "SELECT read_count, unread_count FROM message_read_counters WHERE server_msg_id=?", (first["id"],))
        self.assertEqual((1, 0), tuple(counter))

        saved_cursor = await self.synced(bob_second)
        await self.disconnect(bob_second)
        await bob_second.crash()
        messages = {"multi-read": first}
        for intent, body in (("multi-recall", "remove this"), ("multi-unread", "still unread")):
            await alice_second.command("message", conversation=conversation, intent=intent, text=body)
            messages[intent] = await self.bob.wait("message", lambda item: item["clientMsgId"] == intent)
        recalled = messages["multi-recall"]
        mark = await self.alice.command("recall", conversation=conversation, message=recalled["id"])
        await self.alice.wait("control-writes", lambda item: not item["items"], since=mark)
        await self.bob.wait("update", lambda item: item["type"] == "recall" and item["messageId"] == recalled["id"])
        mark = await self.bob.command("receipt", conversation=conversation, seq=recalled["seq"])
        await self.bob.wait("control-writes", lambda item: not item["items"], since=mark)

        _, user, device, state = devices[3]
        bob_second = await NativeClient.start(self.driver_path,
            self.output_dir / f"{self._testMethodName}-bob-secondary-restarted.log", state)
        self.clients.append(bob_second)
        devices[3] = (bob_second, user, device, state)
        initial = await bob_second.connect(self.endpoint, user, device=device)
        self.assertEqual(saved_cursor, initial["globalCursor"])
        self.assertEqual([first["id"]], [item["id"] for item in initial["recentMessages"]])
        self.assertEqual(0, initial["unreadTotal"])
        self.assertEqual(first["seq"], initial["readProgressByConversation"][conversation]["bob"])
        await self.synced(bob_second)
        self.assertEqual({"multi-recall", "multi-unread"}, {
            item["data"]["clientMsgId"] for item in bob_second.events if item["event"] == "message"})
        before = self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0]
        mark = await bob_second.command("receipt", conversation=conversation, seq=first["seq"])
        await bob_second.wait("control-writes", lambda item: not item["items"], since=mark)
        self.assertEqual(before, self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0])
        await bob_second.command("message", conversation=conversation, intent="multi-reply", text="from second device")
        messages["multi-reply"] = await self.alice.wait("message", lambda item: item["clientMsgId"] == "multi-reply")

        await self.confirmations_settled([(user, state) for _, user, _, state in devices])
        for client, user, device, state in devices:
            await client.wait("message", lambda item: item["clientMsgId"] == "multi-reply")
            cursor = await self.synced(client, user)
            await self.disconnect(client)
            initial = await client.connect(self.endpoint, user, device=device)
            self.assertEqual(cursor, initial["globalCursor"])
            self.assertEqual(1, initial["unreadTotal"])
            self.assertEqual(recalled["seq"], initial["readProgressByConversation"][conversation]["bob"])
            restored = {item["clientMsgId"]: item for item in initial["recentMessages"]}
            self.assertEqual(set(messages), set(restored))
            self.assertEqual(4, len(initial["recentMessages"]))
            emitted = [item["data"]["clientMsgId"] for item in client.events if item["event"] == "message"]
            expected_emitted = set(messages) - ({"multi-read"} if client is bob_second else set())
            self.assertEqual(expected_emitted, set(emitted))
            self.assertEqual(len(expected_emitted), len(emitted))
            self.assertEqual("", restored["multi-recall"]["text"])
            self.assertTrue(restored["multi-recall"]["recalled"])
            for intent in messages:
                self.assertEqual(messages[intent]["id"], restored[intent]["id"])
                self.assertEqual(0 if intent in ("multi-read", "multi-recall") else 1,
                                 restored[intent]["unreadCount"])
            paths = list(state.glob("*.sqlite"))
            self.assertEqual(1, len(paths))
            with closing(sqlite3.connect(paths[0])) as cache:
                seen = cache.execute("SELECT position, event_id FROM seen ORDER BY position").fetchall()
                expected = [tuple(row) for row in self.db.execute_fetchall(
                    "SELECT seq, event_id FROM sync_events WHERE user_id=? ORDER BY seq", (user,))]
                self.assertEqual(expected, seen)
                self.assertEqual(list(range(1, cursor + 1)), [row[0] for row in seen])
                self.assertEqual("ok", cache.execute("PRAGMA integrity_check").fetchone()[0])
        self.assertEqual(4, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])
        self.assertEqual(8, self.db.execute_fetchone("SELECT COUNT(*) FROM message_deliveries")[0])
        self.assertEqual(recalled["seq"], self.db.execute_fetchone(
            "SELECT last_read_seq FROM conversation_members WHERE conversation_id=? AND user_id='bob'",
            (conversation,))[0])


    async def wait_for(self, predicate, timeout=10):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.02)

    def confirmation_metadata(self, state):
        paths = list(state.glob("*.sqlite"))
        if not paths:
            return {}
        with closing(sqlite3.connect(paths[0])) as cache:
            return dict(cache.execute("SELECT key,value FROM metadata"))

    async def confirmations_settled(self, devices=None):
        devices = devices or [("alice", self.root / "state-alice"), ("bob", self.root / "state-bob")]
        def settled():
            for user, state in devices:
                maximum = self.db.execute_fetchone("SELECT COALESCE(MAX(seq),0) FROM sync_events WHERE user_id=?", (user,))[0]
                metadata = self.confirmation_metadata(state)
                if int(metadata.get("sync_confirmed_cursor", 0)) != maximum or metadata.get("sync_confirmation"):
                    return False
            return True
        await self.wait_for(settled, timeout=14)

    def delivered(self, message):
        return dict(self.db.execute_fetchone(
            "SELECT * FROM message_deliveries WHERE server_msg_id=? AND user_id='bob'", (message,)))

    async def test_membership_read_counts_use_original_recipients_and_survive_restart(self):
        repo = ConversationRepo(self.db)
        for user in ("carol", "dave"):
            repo.ensure_user(user)
        mark = await self.alice.command("group", intent="count-group", title="original recipients", members=["carol", "dave"])
        group = (await self.alice.wait("conversation", lambda item: item["type"] == "group", since=mark))["conversationId"]

        async def send_message(intent):
            mark = await self.alice.command("message", conversation=group, intent=intent, text=intent)
            return await self.alice.wait("message", lambda item: item["clientMsgId"] == intent, since=mark)

        async def bob_control(operation, **args):
            mark = await self.bob.command(operation, conversation=group, **args)
            await self.bob.wait("control-writes", lambda item: item["items"] == [], since=mark)

        async def count_changed(message, unread):
            return await self.alice.wait("update", lambda item: item["type"] == "readCount" and
                item["messageId"] == message["id"] and item["unreadCount"] == unread)

        first = await send_message("before-bob-joined")
        await bob_control("join")
        await bob_control("receipt", seq=first["seq"])
        self.assertEqual(2, self.db.execute_fetchone("SELECT unread_count FROM message_read_counters "
            "WHERE server_msg_id=?", (first["id"],))[0])
        second = await send_message("bob-receives")
        await bob_control("receipt", seq=second["seq"])
        await count_changed(second, 2)
        await bob_control("leave")
        absent = await send_message("bob-was-absent")
        await bob_control("join")
        await bob_control("receipt", seq=absent["seq"])
        latest = await send_message("bob-returned")
        await bob_control("receipt", seq=latest["seq"])
        await count_changed(latest, 2)
        pending = await send_message("sender-will-leave")
        mark = await self.alice.command("leave", conversation=group)
        await self.alice.wait("control-writes", lambda item: item["items"] == [], since=mark)
        await bob_control("receipt", seq=pending["seq"])
        await count_changed(pending, 2)
        await self.confirmations_settled()
        await self.alice.crash()
        self.alice = await NativeClient.start(self.driver_path,
            self.output_dir / f"{self._testMethodName}-alice-restarted.log", self.root / "state-alice")
        self.clients.append(self.alice)
        snapshot = await self.alice.connect(self.endpoint, "alice")
        actual = {item["clientMsgId"]: item for item in snapshot["recentMessages"] if item["conversationId"] == group}
        for message in (first, second, absent, latest, pending):
            self.assertEqual(2, actual[message["clientMsgId"]]["unreadCount"])
            self.assertTrue(actual[message["clientMsgId"]]["readCountKnown"])
        self.assertEqual(2, len(self.db.execute_fetchall("SELECT user_id FROM message_deliveries "
            "WHERE server_msg_id=? AND user_id<>'alice'", (absent["id"],))))

    async def test_read_count_migration_corrects_legacy_native_cache_without_resetting_cursor(self):
        await self.alice.command("message", conversation=self.conversation, intent="legacy-count", text="kept history")
        message = await self.bob.wait("message", lambda item: item["clientMsgId"] == "legacy-count")
        await self.bob.command("receipt", conversation=self.conversation, seq=message["seq"])
        await self.alice.wait("update", lambda item: item["type"] == "readCount" and item["unreadCount"] == 0)
        await self.confirmations_settled()
        await self.alice.crash()
        cache_path = next((self.root / "state-alice").glob("*.sqlite"))
        with closing(sqlite3.connect(cache_path)) as cache, cache:
            before_cursor = int(cache.execute("SELECT value FROM metadata WHERE key='cursor'").fetchone()[0])
            cache.execute("DELETE FROM objects WHERE kind='readCount'")
            cache.execute("UPDATE objects SET data=json_set(data,'$.unreadCount',1) WHERE kind='message'")
        self.db.execute_write("DELETE FROM schema_migrations WHERE name='read_count_events_v1'")
        init_db(self.db_path)
        self.alice = await NativeClient.start(self.driver_path,
            self.output_dir / f"{self._testMethodName}-alice-upgraded.log", self.root / "state-alice")
        self.clients.append(self.alice)
        initial = await self.alice.connect(self.endpoint, "alice")
        self.assertEqual(before_cursor, initial["globalCursor"])
        self.assertFalse(initial["recentMessages"][0]["readCountKnown"])
        corrected = await self.alice.wait("update", lambda item: item["type"] == "readCount" and
            item["messageId"] == message["id"] and item["globalSeq"] > before_cursor)
        self.assertEqual(0, corrected["unreadCount"])
        await self.synced(self.alice, "alice")
        await self.disconnect(self.alice)
        restored = await self.alice.connect(self.endpoint, "alice")
        self.assertEqual(0, restored["recentMessages"][0]["unreadCount"])
        self.assertEqual("kept history", restored["recentMessages"][0]["text"])
        self.assertTrue(restored["recentMessages"][0]["readCountKnown"])
        self.assertGreater(restored["globalCursor"], before_cursor)

    async def test_native_delivery_waits_for_offline_receiver_and_restores_sender_display(self):
        await self.disconnect(self.bob)
        await self.alice.command("message", conversation=self.conversation, intent="delivery-offline", text="offline")
        message = await self.alice.wait("message", lambda item: item["clientMsgId"] == "delivery-offline")
        self.assertEqual("sent", self.delivered(message["id"])["status"])
        self.assertIsNone(self.delivered(message["id"])["delivered_at_ms"])
        await self.bob.connect(self.endpoint, "bob")
        delivered = await self.alice.wait("update", lambda item: item["type"] == "delivery" and item["messageId"] == message["id"])
        self.assertEqual("bob", delivered["userId"])
        self.assertEqual("delivered", delivered["status"])
        self.assertEqual(self.delivered(message["id"])["delivered_at_ms"], delivered["deliveredAtMs"])
        self.assertIsNone(self.delivered(message["id"])["read_at_ms"])
        await self.confirmations_settled()
        await self.disconnect(self.alice)
        snapshot = await self.alice.connect(self.endpoint, "alice")
        self.assertEqual([delivered], snapshot["deliveries"])
        self.assertEqual(1, snapshot["recentMessages"][0]["unreadCount"])
        await self.bob.command("receipt", conversation=self.conversation, seq=message["seq"])
        await self.alice.wait("update", lambda item: item["type"] == "receipt")
        self.assertEqual("read", self.delivered(message["id"])["status"])

    async def test_native_delivery_lost_ack_retries_original_request(self):
        await self.confirmations_settled()
        self.drop_confirmation_acks = 1
        await self.alice.command("message", conversation=self.conversation, intent="delivery-retry", text="retry")
        message = await self.bob.wait("message", lambda item: item["clientMsgId"] == "delivery-retry")
        await self.alice.wait("update", lambda item: item["type"] == "delivery" and item["messageId"] == message["id"])
        original = json.loads(self.confirmation_metadata(self.root / "state-bob")["sync_confirmation"])
        row = self.delivered(message["id"])
        await self.confirmations_settled()
        attempts = [item for item in self.confirmation_attempts if item["requestId"] == original["requestId"]]
        self.assertEqual(2, len(attempts))
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(row, self.delivered(message["id"]))
        self.assertEqual(1, sum(packet["event"] == "update" and packet["data"].get("type") == "delivery"
                                for packet in self.alice.events))

    async def test_native_delivery_pending_confirmation_survives_process_restart(self):
        await self.confirmations_settled()
        self.reject_confirmation = True
        await self.alice.command("message", conversation=self.conversation, intent="delivery-crash", text="persist")
        message = await self.bob.wait("message", lambda item: item["clientMsgId"] == "delivery-crash")
        await self.bob.wait("error", lambda item: "confirmation rejected temporarily" in item["message"])
        pending = json.loads(self.confirmation_metadata(self.root / "state-bob")["sync_confirmation"])
        self.assertEqual("sent", self.delivered(message["id"])["status"])
        await self.bob.crash()
        self.reject_confirmation = False
        snapshot = await self.restart_bob()
        self.assertEqual(message["id"], snapshot["recentMessages"][0]["id"])
        await self.alice.wait("update", lambda item: item["type"] == "delivery" and item["messageId"] == message["id"])
        await self.confirmations_settled()
        attempts = [item for item in self.confirmation_attempts if item["requestId"] == pending["requestId"]]
        self.assertEqual(2, len(attempts))
        self.assertEqual(attempts[0], attempts[1])

    async def test_native_delivery_does_not_confirm_past_sync_gap(self):
        await self.confirmations_settled()
        before = int(self.confirmation_metadata(self.root / "state-bob")["sync_confirmed_cursor"])
        self.hold_bob_history = True
        self.drop_next_bob_message = True
        for intent in ("delivery-missing", "delivery-later"):
            await self.alice.command("message", conversation=self.conversation, intent=intent, text=intent)
            await self.alice.wait("message", lambda item: item["clientMsgId"] == intent)
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "delivery-later")
        await self.wait_for(lambda: self.held_history_count > 0)
        await asyncio.sleep(0.8)
        self.assertEqual(before, int(self.confirmation_metadata(self.root / "state-bob")["sync_confirmed_cursor"]))
        self.assertEqual(["sent", "sent"], [row[0] for row in self.db.execute_fetchall(
            "SELECT status FROM message_deliveries WHERE user_id='bob' ORDER BY seq")])
        self.hold_bob_history = False
        await self.confirmations_settled()
        self.assertEqual(["delivered", "delivered"], [row[0] for row in self.db.execute_fetchall(
            "SELECT status FROM message_deliveries WHERE user_id='bob' ORDER BY seq")])

    async def test_native_delivery_save_failure_never_reaches_server(self):
        await self.confirmations_settled()
        cache_path = next((self.root / "state-bob").glob("*.sqlite"))
        with closing(sqlite3.connect(cache_path)) as cache:
            cache.execute("CREATE TRIGGER reject_confirmation BEFORE INSERT ON metadata WHEN NEW.key='sync_confirmation' "
                          "BEGIN SELECT RAISE(ABORT,'injected confirmation save failure'); END")
            cache.commit()
        count = len([item for item in self.confirmation_attempts if item["user"] == "bob"])
        mark = await self.alice.command("message", conversation=self.conversation, intent="delivery-save", text="saved message")
        message = await self.alice.wait("message", lambda item: item["clientMsgId"] == "delivery-save", since=mark)
        await self.bob.wait("error", lambda item: "injected confirmation save failure" in item["message"])
        await self.bob.wait("connection", lambda item: item["state"] == "disconnected")
        self.assertEqual(count, len([item for item in self.confirmation_attempts if item["user"] == "bob"]))
        self.assertEqual("sent", self.delivered(message["id"])["status"])
        with closing(sqlite3.connect(cache_path)) as cache:
            cache.execute("DROP TRIGGER reject_confirmation")
            cache.commit()
        await self.bob.connect(self.endpoint, "bob")
        await self.alice.wait("update", lambda item: item["type"] == "delivery" and item["messageId"] == message["id"])
        await self.confirmations_settled()

    async def test_message_receipt_recall_and_offline_sync(self):
        mark = await self.alice.command("message", conversation=self.conversation, intent="message-1", text="hello")
        sent = await self.alice.wait("message", lambda item: item["clientMsgId"] == "message-1", since=mark)
        received = await self.bob.wait("message", lambda item: item["clientMsgId"] == "message-1")
        self.assertEqual("hello", received["text"])
        self.assertEqual(sent["id"], received["id"])
        await self.alice.command("message", conversation=self.conversation, intent="message-1", text="hello")
        mark = len(self.alice.events)
        await self.bob.command("receipt", conversation=self.conversation, seq=sent["seq"])
        await self.alice.wait("update", lambda item: item["type"] == "receipt", since=mark)
        mark = len(self.bob.events)
        await self.alice.command("recall", conversation=self.conversation, message=sent["id"])
        await self.bob.wait("update", lambda item: item["type"] == "recall" and item["messageId"] == sent["id"], since=mark)
        self.assertEqual(1, self.db.execute_fetchone(
            "SELECT COUNT(*) FROM messages WHERE client_msg_id = ?", ("message-1",),
        )[0])
        mark = await self.bob.command("disconnect")
        await self.bob.wait("connection", lambda item: item["state"] == "disconnected", since=mark)
        await self.alice.command("message", conversation=self.conversation, intent="offline", text="while offline")
        await self.alice.wait("message", lambda item: item["clientMsgId"] == "offline")
        mark = len(self.bob.events)
        await self.bob.connect(self.endpoint, "bob")
        caught_up = await self.bob.wait("message", lambda item: item["clientMsgId"] == "offline", since=mark)
        self.assertEqual("while offline", caught_up["text"])

    async def synced(self, client, user="bob"):
        cursor = self.db.execute_fetchone(
            "SELECT COALESCE(MAX(seq), 0) FROM sync_events WHERE user_id = ?", (user,),
        )[0]
        await client.wait("sync", lambda item: item["globalCursor"] == cursor and not item["hasGap"])
        return cursor

    async def restart_bob(self):
        client = await NativeClient.start(
            self.driver_path, self.output_dir / f"{self._testMethodName}-bob-restarted.log",
            self.root / "state-bob",
        )
        self.clients.append(client)
        self.bob = client
        try:
            return await client.connect(self.endpoint, "bob")
        except Exception:
            transport = self.server._transport
            reading = getattr(transport, "_read_fut", None)
            self.server_log.write(
                f"Restart failure: closing={transport.is_closing()} "
                f"read_pending={reading is not None and not reading.done()} "
                f"protocols={len(self.protocols)}\n"
            )
            self.server_log.flush()
            raise

    async def disconnect(self, client):
        mark = await client.command("disconnect")
        await client.wait("connection", lambda item: item["state"] == "disconnected", since=mark)

    async def test_control_receipt_survives_restart_before_server_commit(self):
        await self.alice.command("message", conversation=self.conversation, intent="unread-control", text="read me")
        message = await self.bob.wait("message", lambda item: item["clientMsgId"] == "unread-control")
        self.reject_control_code = 503
        mark = await self.bob.command("receipt", conversation=self.conversation, seq=message["seq"])
        await self.bob.wait("error", lambda item: "injected control write failure" in item["message"], since=mark)
        original = [item for item in self.control_attempts if item["operation"] == "receipt"][-1]
        await self.bob.crash()
        self.reject_control_code = 0
        mark = len(self.alice.events)
        await self.restart_bob()
        await self.alice.wait("update", lambda item: item["type"] == "receipt", since=mark, timeout=8)
        attempts = [item for item in self.control_attempts if item["operation"] == "receipt"]
        self.assertEqual(2, len(attempts))
        self.assertEqual(original, attempts[-1])
        self.assertEqual(message["seq"], self.db.execute_fetchone(
            "SELECT last_read_seq FROM conversation_members WHERE conversation_id=? AND user_id='bob'",
            (self.conversation,))[0])

    async def test_control_timeout_retry_does_not_repeat_later_membership_changes(self):
        mark = await self.alice.command("group", intent="control-group", title="members", members=["bob"])
        group = (await self.alice.wait("conversation", lambda item: item["type"] == "group", since=mark))["conversationId"]
        self.drop_control_acks = 1
        self.duplicate_control_acks = True
        mark = await self.bob.command("leave", conversation=group)
        left = await self.bob.wait("conversation", lambda item: item["conversationId"] == group and "bob" not in item["memberIds"], since=mark)
        await self.bob.command("join", conversation=group)
        await self.bob.wait("control-writes", lambda item: item["items"] == [], since=mark, timeout=8)
        joined = await self.bob.wait("conversation", lambda item: item["conversationId"] == group and "bob" in item["memberIds"], since=mark)
        attempts = [item for item in self.control_attempts if item["user"] == "bob"]
        self.assertEqual(["leave_conversation", "leave_conversation", "join_conversation"], [item["operation"] for item in attempts])
        self.assertEqual(attempts[0], attempts[1])
        self.assertNotEqual(attempts[1]["requestId"], attempts[2]["requestId"])
        self.assertEqual(1, sum(item["event"] == "conversation" and item["data"] == left for item in self.bob.events))
        self.assertIn("bob", joined["memberIds"])

    async def test_control_create_lost_ack_restarts_without_duplicate_conversation_events(self):
        self.drop_control_acks = 1
        mark = await self.bob.command("group", intent="recover-group", title="persisted", members=["alice"])
        group = await self.bob.wait("conversation", lambda item: item["title"] == "persisted", since=mark)
        original = [item for item in self.control_attempts if item["user"] == "bob"][-1]
        count = self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0]
        await self.bob.crash()
        initial = await self.restart_bob()
        self.assertEqual(original["requestId"], initial["controlWrites"][0]["requestId"])
        await self.bob.wait("control-writes", lambda item: item["items"] == [])
        self.assertEqual(count, self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0])
        attempts = [item for item in self.control_attempts if item["user"] == "bob"]
        self.assertEqual([original, original], attempts)
        self.assertEqual(group["conversationId"], self.db.execute_fetchone(
            "SELECT conversation_id FROM conversation_create_requests WHERE user_id='bob' AND client_conv_id='recover-group'")[0])

    async def test_control_recall_waits_for_sync_and_survives_account_switch(self):
        mark = await self.bob.command("message", conversation=self.conversation, intent="recall-control", text="recall me")
        message = await self.bob.wait("message", lambda item: item["clientMsgId"] == "recall-control", since=mark)
        self.hold_bob_history = True
        await self.disconnect(self.bob)
        await self.bob.connect(self.endpoint, "bob")
        mark = await self.bob.command("recall", conversation=self.conversation, message=message["id"])
        pending = await self.bob.wait("control-writes", lambda item: bool(item["items"]), since=mark)
        request = pending["items"][0]["requestId"]
        self.assertFalse(any(item["requestId"] == request for item in self.control_attempts))
        await self.disconnect(self.bob)
        other = await self.bob.connect(self.endpoint, "alice")
        self.assertEqual([], other["controlWrites"])
        await self.synced(self.bob, "alice")
        self.assertFalse(any(item["requestId"] == request for item in self.control_attempts))
        await self.disconnect(self.bob)
        self.hold_bob_history = False
        mark = len(self.alice.events)
        restored = await self.bob.connect(self.endpoint, "bob")
        self.assertEqual(request, restored["controlWrites"][0]["requestId"])
        await self.alice.wait("update", lambda item: item["type"] == "recall" and item["messageId"] == message["id"], since=mark)
        self.assertEqual(["bob"], [item["user"] for item in self.control_attempts if item["requestId"] == request])

    async def test_control_members_and_rename_restore_saved_queue_order(self):
        mark = await self.alice.command("group", intent="member-queue", title="before", members=["bob"])
        group = (await self.alice.wait("conversation", lambda item: item["title"] == "before", since=mark))["conversationId"]
        self.reject_control_code = 503
        mark = await self.alice.command("remove-members", conversation=group, members=["bob"])
        await self.alice.wait("error", lambda item: "injected control" in item["message"], since=mark)
        await self.alice.command("add-members", conversation=group, members=["bob"])
        await self.alice.command("rename", conversation=group, title="after")
        pending = await self.alice.wait("control-writes", lambda item: len(item["items"]) == 3, since=mark)
        requests = [item["requestId"] for item in pending["items"]]
        await self.alice.crash()
        self.reject_control_code = 0
        self.alice = await NativeClient.start(self.driver_path,
            self.output_dir / f"{self._testMethodName}-alice-restarted.log", self.root / "state-alice")
        self.clients.append(self.alice)
        initial = await self.alice.connect(self.endpoint, "alice")
        self.assertEqual(requests, [item["requestId"] for item in initial["controlWrites"]])
        await self.alice.wait("control-writes", lambda item: item["items"] == [])
        await self.bob.wait("conversation", lambda item: item["conversationId"] == group and item["title"] == "after")
        attempts = [item["requestId"] for item in self.control_attempts if item["requestId"] in requests]
        self.assertEqual([requests[0], *requests], attempts)
        self.assertEqual(["alice", "bob"], [row[0] for row in self.db.execute_fetchall(
            "SELECT user_id FROM conversation_members WHERE conversation_id=? ORDER BY user_id", (group,))])

    async def test_control_local_insert_failure_rejects_without_sending(self):
        await self.synced(self.bob)
        with closing(sqlite3.connect(next((self.root / "state-bob").glob("*.sqlite")))) as cache, cache:
            cache.execute("CREATE TRIGGER reject_control_insert BEFORE INSERT ON control_outbox "
                          "BEGIN SELECT RAISE(ABORT, 'injected local control failure'); END")
        before = len(self.control_attempts)
        with self.assertRaisesRegex(AssertionError, "native command rejected"):
            await self.bob.command("receipt", conversation=self.conversation, seq=0)
        self.assertEqual(before, len(self.control_attempts))
        with closing(sqlite3.connect(next((self.root / "state-bob").glob("*.sqlite")))) as cache, cache:
            self.assertEqual(0, cache.execute("SELECT COUNT(*) FROM control_outbox").fetchone()[0])
            cache.execute("DROP TRIGGER reject_control_insert")
        await self.bob.command("receipt", conversation=self.conversation, seq=0)
        await self.bob.wait("control-writes", lambda item: item["items"] == [])

    async def test_control_terminal_rejection_survives_restart_and_new_action_uses_new_id(self):
        # Bob cannot rename Alice's group; the direct conversation also rejects group rename.
        mark = await self.bob.command("rename", conversation=self.conversation, title="not allowed")
        rejected = await self.bob.wait("control-writes", lambda item: item["items"] and item["items"][0]["status"] == "failed", since=mark)
        request = rejected["items"][0]["requestId"]
        await self.bob.crash()
        initial = await self.restart_bob()
        self.assertEqual("failed", initial["controlWrites"][0]["status"])
        await self.synced(self.bob)
        mark = await self.bob.command("rename", conversation=self.conversation, title="new action")
        results = await self.bob.wait("control-writes", lambda item: len(item["items"]) == 2 and all(
            entry["status"] == "failed" for entry in item["items"]), since=mark)
        self.assertEqual(2, len(set(item["requestId"] for item in results["items"])))
        self.assertEqual(1, sum(item["requestId"] == request for item in self.control_attempts))

    async def test_process_restart_restores_cache_receipts_and_recall(self):
        await self.alice.command("message", conversation=self.conversation, intent="retained", text="cached body")
        retained = await self.bob.wait("message", lambda item: item["clientMsgId"] == "retained")
        await self.bob.command("receipt", conversation=self.conversation, seq=retained["seq"])
        await self.bob.wait("update", lambda item: item["type"] == "receipt")
        await self.alice.command("message", conversation=self.conversation, intent="recalled", text="remove body")
        recalled = await self.bob.wait("message", lambda item: item["clientMsgId"] == "recalled")
        await self.alice.command("recall", conversation=self.conversation, message=recalled["id"])
        await self.bob.wait("update", lambda item: item["type"] == "recall" and item["messageId"] == recalled["id"])
        saved_cursor = await self.synced(self.bob)
        await self.bob.crash()

        await self.alice.command("message", conversation=self.conversation, intent="offline-restart", text="after crash")
        await self.alice.wait("message", lambda item: item["clientMsgId"] == "offline-restart")
        initial = await self.restart_bob()
        self.assertEqual(saved_cursor, initial["globalCursor"])
        restored = {item["clientMsgId"]: item for item in initial["recentMessages"]}
        self.assertEqual({"retained", "recalled"}, set(restored))
        self.assertEqual("cached body", restored["retained"]["text"])
        self.assertEqual("", restored["recalled"]["text"])
        self.assertTrue(restored["recalled"]["recalled"])
        self.assertEqual(retained["seq"], initial["readProgressByConversation"][self.conversation]["bob"])
        self.assertEqual(0, initial["unreadTotal"])
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "offline-restart")
        await self.synced(self.bob)
        emitted = [packet["data"]["clientMsgId"] for packet in self.bob.events if packet["event"] == "message"]
        self.assertEqual(["offline-restart"], emitted)

    async def test_process_restart_fills_persisted_sync_gap(self):
        saved_cursor = await self.synced(self.bob)
        self.drop_next_bob_message = True
        self.hold_bob_history = True
        await self.alice.command("message", conversation=self.conversation, intent="missing", text="missing history")
        await self.alice.wait("message", lambda item: item["clientMsgId"] == "missing")
        await self.alice.command("message", conversation=self.conversation, intent="later", text="later online")
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "later")
        await self.bob.wait("sync", lambda item: item["hasGap"] and item["globalCursor"] == saved_cursor)
        async with asyncio.timeout(5):
            while self.held_history_count == 0:
                await asyncio.sleep(0.001)
        await self.bob.crash()
        self.hold_bob_history = False

        initial = await self.restart_bob()
        self.assertEqual(saved_cursor, initial["globalCursor"])
        self.assertEqual(["later"], [item["clientMsgId"] for item in initial["recentMessages"]])
        missing = await self.bob.wait("message", lambda item: item["clientMsgId"] == "missing")
        self.assertEqual("missing history", missing["text"])
        self.assertGreater(await self.synced(self.bob), saved_cursor)
        emitted = [packet["data"]["clientMsgId"] for packet in self.bob.events if packet["event"] == "message"]
        self.assertEqual(["missing"], emitted)

    async def test_sync_history_spans_two_pages_without_duplicate_delivery(self):
        await self.synced(self.bob)
        await self.disconnect(self.bob)
        intents = [f"history-{index}" for index in range(205)]
        for intent in intents:
            await self.alice.command("message", conversation=self.conversation, intent=intent, text=intent)
        await self.alice.wait("message", lambda item: item["clientMsgId"] == intents[-1], timeout=15)
        start = len(self.sync_replies)
        mark = len(self.bob.events)
        await self.bob.connect(self.endpoint, "bob")
        await self.bob.wait("message", lambda item: item["clientMsgId"] == intents[-1], since=mark)
        await self.synced(self.bob)
        delivered = [packet["data"]["clientMsgId"] for packet in self.bob.events[mark:] if packet["event"] == "message"]
        self.assertEqual(intents, delivered)
        pages = self.sync_replies[start:]
        self.assertEqual([200, 5], [len(page["positions"]) for page in pages])
        self.assertNotEqual(pages[0]["requestId"], pages[1]["requestId"])
        self.assertEqual(pages[0]["positions"][-1] + 1, pages[1]["positions"][0])

    async def test_empty_sync_page_retries_without_releasing_pending_writes(self):
        cursor = await self.synced(self.bob)
        reply_start = len(self.sync_replies)
        self.drop_next_bob_message = True
        self.empty_bob_history_count = 1
        await self.alice.command("message", conversation=self.conversation, intent="gap-missing", text="missing")
        await self.alice.wait("message", lambda item: item["clientMsgId"] == "gap-missing")
        mark = await self.alice.command("message", conversation=self.conversation, intent="gap-later", text="later")
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "gap-later")
        await self.bob.wait("error", lambda item: "unresolved gap" in item["message"])
        await self.bob.command("message", conversation=self.conversation, intent="after-gap", text="queued")
        source = self.root / "after-gap.bin"
        source.write_bytes(b"file waits for complete history")
        await self.bob.command("upload", conversation=self.conversation, path=str(source))
        self.assertFalse(any(item["user"] == "bob" for item in self.message_attempts))
        self.assertFalse(any(item["user"] == "bob" for item in self.file_attempts))
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "gap-missing", timeout=8)
        await self.alice.wait("message", lambda item: item["clientMsgId"] == "after-gap", since=mark)
        await self.bob.wait("file-tasks", lambda item: not item["items"])
        self.assertGreater(await self.synced(self.bob), cursor)
        replies = self.sync_replies[reply_start:]
        self.assertTrue(replies[0]["empty"])
        self.assertGreaterEqual(len(replies), 2)
        self.assertEqual(replies[0]["requestId"], replies[1]["requestId"])
        emitted = [event["data"]["clientMsgId"] for event in self.bob.events if event["event"] == "message"]
        self.assertEqual(1, emitted.count("gap-missing"))
        self.assertEqual(1, emitted.count("gap-later"))

    async def test_switch_users_in_one_process_isolates_and_restores_cache(self):
        await self.alice.command("message", conversation=self.conversation, intent="bob-only", text="bob history")
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "bob-only")
        bob_cursor = await self.synced(self.bob)
        await self.disconnect(self.bob)
        carol_initial = await self.bob.connect(self.endpoint, "carol")
        self.assertEqual(0, carol_initial["globalCursor"])
        self.assertEqual([], carol_initial["recentMessages"])
        self.assertEqual([], carol_initial["conversations"])
        self.assertEqual({}, carol_initial["readProgressByConversation"])
        self.assertEqual([], carol_initial["files"])

        mark = await self.alice.command("direct", intent="carol-conversation", peer="carol")
        conversation = await self.alice.wait("conversation", lambda item: "carol" in item["memberIds"], since=mark)
        carol_conversation = conversation["conversationId"]
        await self.bob.wait("conversation", lambda item: item["conversationId"] == carol_conversation)
        await self.alice.command("message", conversation=carol_conversation, intent="carol-only", text="carol history")
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "carol-only")
        await self.synced(self.bob, "carol")
        await self.disconnect(self.bob)
        bob_initial = await self.bob.connect(self.endpoint, "bob")
        self.assertEqual(bob_cursor, bob_initial["globalCursor"])
        self.assertEqual(["bob-only"], [item["clientMsgId"] for item in bob_initial["recentMessages"]])
        self.assertEqual([self.conversation], [item["conversationId"] for item in bob_initial["conversations"]])
        self.assertEqual(1, bob_initial["unreadTotal"])

    async def restart_alice(self):
        client = await NativeClient.start(
            self.driver_path, self.output_dir / f"{self._testMethodName}-alice-restarted.log",
            self.root / "state-alice",
        )
        self.clients.append(client)
        self.alice = client
        return await client.connect(self.endpoint, "alice")

    def assert_single_message_intent(self, intent, attempts):
        recorded = [item for item in self.message_attempts if item["intent"] == intent]
        self.assertEqual(attempts, len(recorded))
        self.assertEqual(1, len({item["requestId"] for item in recorded}))
        self.assertEqual(1, len({item["body"] for item in recorded}))
        self.assertEqual(1, self.db.execute_fetchone(
            "SELECT COUNT(*) FROM messages WHERE client_msg_id=?", (intent,),
        )[0])
        return recorded[0]["requestId"]

    async def test_second_device_message_intent_conflict_is_durable(self):
        mark = await self.alice.command("message", conversation=self.conversation, intent="shared-message", text="original")
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "shared-message")
        await self.alice.wait("message-sends", lambda item: not item["items"], since=mark)
        cache_root = self.root / "state-alice-conflict"
        second = await NativeClient.start(self.driver_path, self.output_dir / "intent-second.log", cache_root)
        self.clients.append(second)
        await second.connect(self.endpoint, "alice", "device-alice-conflict")
        mark = await second.command("message", conversation=self.conversation, intent="shared-message", text="different")
        failed = await second.wait("message-sends", lambda data: any(item["code"] == 409 for item in data["items"]), since=mark)
        pending = next(item for item in failed["items"] if item["code"] == 409)
        self.assertEqual("failed", pending["status"])
        self.assertEqual("different", pending["text"])
        await second.crash()
        restored = await NativeClient.start(self.driver_path, self.output_dir / "intent-restored.log", cache_root)
        self.clients.append(restored)
        initial = await restored.connect(self.endpoint, "alice", "device-alice-conflict")
        self.assertEqual(pending["requestId"], initial["messageSends"][0]["requestId"])
        mark = await restored.command("retry-message", conversation=self.conversation, intent="shared-message")
        await restored.wait("message-sends", lambda data: any(
            item["requestId"] == pending["requestId"] and item["code"] == 409
            and item["attempts"] > pending["attempts"] for item in data["items"]), since=mark)
        self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])
        self.assertEqual(b"original", self.db.execute_fetchone("SELECT content FROM messages")[0])
        attempts = [item for item in self.message_attempts if item["requestId"] == pending["requestId"]]
        self.assertEqual(2, len(attempts))
        self.assertEqual(1, len({item["body"] for item in attempts}))

    async def test_message_outbox_restarts_after_committed_but_missing_ack(self):
        self.drop_message_ack_count = 1
        mark = await self.alice.command("message", conversation=self.conversation, intent="ack-lost", text="write once")
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "ack-lost")
        await self.alice.wait("message-sends", lambda data: any(item["clientMsgId"] == "ack-lost"
                              for item in data["items"]), since=mark)
        await self.alice.crash()
        initial = await self.restart_alice()
        self.assertEqual(["ack-lost"], [item["clientMsgId"] for item in initial["messageSends"]])
        await self.alice.wait("message-sends", lambda data: not data["items"])
        self.assert_single_message_intent("ack-lost", 2)
        import sqlite3
        cache = next((self.root / "state-alice").glob("*.sqlite"))
        with closing(sqlite3.connect(cache)) as connection:
            status, payload = connection.execute("SELECT status,payload FROM message_outbox").fetchone()
        self.assertEqual("confirmed", status)
        self.assertEqual(b"", payload)

    async def test_message_outbox_restarts_before_server_commit(self):
        self.rejected_message_intents["before-commit"] = 503
        mark = await self.alice.command("message", conversation=self.conversation, intent="before-commit", text="recover me")
        await self.alice.wait("message-sends", lambda data: any(item["code"] == 503 for item in data["items"]), since=mark)
        self.assertEqual(0, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])
        await self.alice.crash()
        self.rejected_message_intents.clear()
        initial = await self.restart_alice()
        self.assertEqual("recover me", initial["messageSends"][0]["text"])
        self.assertEqual("pending", initial["messageSends"][0]["status"])
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "before-commit")
        await self.alice.wait("message-sends", lambda data: not data["items"])
        self.assert_single_message_intent("before-commit", 2)

    async def test_message_timeout_retry_and_duplicate_ack_preserve_queue_order(self):
        self.drop_message_ack_count = 1
        self.duplicate_message_acks = True
        await self.alice.command("message", conversation=self.conversation, intent="first-pending", text="first")
        await self.alice.command("message", conversation=self.conversation, intent="second-pending", text="second")
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "second-pending")
        await self.alice.wait("message-sends", lambda data: not data["items"])
        self.assert_single_message_intent("first-pending", 2)
        self.assert_single_message_intent("second-pending", 1)
        self.assertEqual(["first-pending", "first-pending", "second-pending"],
                         [item["intent"] for item in self.message_attempts])

    async def test_rejected_message_survives_restart_for_explicit_retry(self):
        self.rejected_message_intents["rejected"] = 403
        mark = await self.alice.command("message", conversation=self.conversation, intent="rejected", text="keep failed text")
        await self.alice.wait("message-sends", lambda data: any(item["status"] == "failed"
                              for item in data["items"]), since=mark)
        await self.alice.crash()
        initial = await self.restart_alice()
        self.assertEqual("failed", initial["messageSends"][0]["status"])
        self.assertEqual("keep failed text", initial["messageSends"][0]["text"])
        await self.synced(self.alice, "alice")
        self.assertEqual(1, len(self.message_attempts))
        self.rejected_message_intents.clear()
        mark = await self.alice.command("retry-message", conversation=self.conversation, intent="rejected")
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "rejected")
        await self.alice.wait("message-sends", lambda data: not data["items"], since=mark)
        self.assert_single_message_intent("rejected", 2)

    async def test_unconfirmed_messages_stay_with_original_account(self):
        self.rejected_message_intents["alice-pending"] = 503
        mark = await self.alice.command("message", conversation=self.conversation, intent="alice-pending", text="alice only")
        await self.alice.wait("message-sends", lambda data: any(item["code"] == 503 for item in data["items"]), since=mark)
        await self.disconnect(self.alice)
        self.rejected_message_intents.clear()
        initial = await self.alice.connect(self.endpoint, "carol")
        self.assertEqual([], initial["messageSends"])
        await self.synced(self.alice, "carol")
        self.assertEqual(1, len(self.message_attempts))
        self.assertEqual(0, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])
        await self.disconnect(self.alice)
        restored = await self.alice.connect(self.endpoint, "alice")
        self.assertEqual(["alice-pending"], [item["clientMsgId"] for item in restored["messageSends"]])
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "alice-pending")
        await self.alice.wait("message-sends", lambda data: not data["items"],
                              since=next(i for i, event in enumerate(self.alice.events)
                                         if event["event"] == "initial" and event["data"] == restored))
        self.assert_single_message_intent("alice-pending", 2)
        self.assertEqual({"alice"}, {item["user"] for item in self.message_attempts})

    def active_protocol(self, user):
        return next(protocol for protocol in reversed(self.protocols)
                    if protocol.m_user_id == user and not protocol._closed.is_set())

    async def test_paged_cache_history_keeps_unread_and_terminal_state_after_restart(self):
        sent = []
        for index in range(125):
            intent = f"history-{index}"
            mark = await self.bob.command("message", conversation=self.conversation, intent=intent, text=intent)
            sent.append(await self.alice.wait("message", lambda item: item["clientMsgId"] == intent))
            await self.bob.wait("message-sends", lambda item: not item["items"], since=mark)
        await self.confirmations_settled()
        await self.alice.crash()
        initial = await self.restart_alice()
        self.assertEqual(50, len(initial["recentMessages"]))
        self.assertEqual(125, initial["unreadTotal"])
        self.assertTrue(initial["unreadAuthoritative"])
        oldest = sent[0]
        mark = await self.bob.command("recall", conversation=self.conversation, message=oldest["id"])
        await self.bob.wait("control-writes", lambda item: not item["items"], since=mark)
        await self.alice.wait("update", lambda item: item.get("messageId") == oldest["id"] and item["type"] == "recall")
        await self.alice.wait("sync", lambda item: item.get("unreadTotal") == 124)
        history = initial["historyByConversation"][self.conversation]
        messages = {item["id"]: item for item in initial["recentMessages"]}
        while history["hasMore"]:
            mark = await self.alice.command("history", conversation=self.conversation, cursor=history["cursor"])
            history = await self.alice.wait("history", since=mark)
            self.assertTrue(history["ok"])
            self.assertLessEqual(len(history["messages"]), 50)
            for item in history["messages"]:
                self.assertNotIn(item["id"], messages)
                messages[item["id"]] = item
        self.assertEqual({item["id"] for item in sent}, set(messages))
        self.assertTrue(messages[oldest["id"]]["recalled"])
        self.assertEqual("", messages[oldest["id"]]["text"])
        mark = await self.alice.command("receipt", conversation=self.conversation, seq=100)
        await self.alice.wait("control-writes", lambda item: not item["items"], since=mark)
        await self.alice.wait("sync", lambda item: item.get("unreadTotal") == 25)
        await self.alice.command("disconnect")
        mark = await self.alice.command("history", conversation=self.conversation, cursor="")
        self.assertFalse((await self.alice.wait("history", since=mark))["ok"])

    async def test_queue_overload_resumes_original_upload_after_committed_progress(self):
        source = self.root / "overload-upload.bin"
        payload = bytes(range(251)) * 4096
        source.write_bytes(payload)
        original_limit = self.hub.writes.max_payload_bytes
        original_append = self.files.append_file_chunk
        injected = False
        def append(*args, **kwargs):
            nonlocal injected
            result = original_append(*args, **kwargs)
            if not injected:
                injected = True
                self.hub.writes.max_payload_bytes = 1
            return result
        try:
            with patch.object(self.files, "append_file_chunk", append):
                mark = await self.alice.command("upload", conversation=self.conversation, path=str(source))
                await self.alice.wait("connection", lambda item: item["state"] == "disconnected", since=mark)
            progress = self.db.execute_fetchone("SELECT received_bytes FROM file_transfers")[0]
            self.assertGreater(progress, 0)
            self.assertLess(progress, len(payload))
            self.assertEqual(0, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])
        finally:
            self.hub.writes.max_payload_bytes = original_limit
        await self.alice.wait("file", lambda item: item["completed"], since=mark)
        row, attempts = self.assert_single_file_intent("alice", 1)
        self.assertEqual(progress, attempts[-1]["acceptedOffset"])
        self.assertEqual(payload, self.files.get_storage_path(row["file_id"]).read_bytes())
        self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])

    async def test_queue_overload_reconnects_with_original_saved_message(self):
        original_limit = self.hub.writes.max_payload_bytes
        self.hub.writes.max_payload_bytes = 1
        try:
            mark = await self.alice.command("message", conversation=self.conversation,
                intent="overload-message", text="retry after rejected input")
            pending = await self.alice.wait("message-sends", lambda item: any(
                row["clientMsgId"] == "overload-message" for row in item["items"]), since=mark)
            identity = next(row for row in pending["items"] if row["clientMsgId"] == "overload-message")
            await self.alice.wait("connection", lambda item: item["state"] == "disconnected", since=mark)
            self.assertEqual(0, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])
        finally:
            self.hub.writes.max_payload_bytes = original_limit
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "overload-message")
        await self.alice.wait("message-sends", lambda item: not item["items"], since=mark)
        self.assertEqual(1, len(self.message_attempts))
        self.assertEqual(identity["requestId"], self.message_attempts[0]["requestId"])
        self.assert_single_message_intent("overload-message", 1)

    async def test_connection_drop_resends_unconfirmed_message_automatically(self):
        self.drop_message_ack_count = 1
        await self.alice.command("message", conversation=self.conversation, intent="reconnect-intent", text="recover")
        await self.alice.wait("message", lambda item: item["clientMsgId"] == "reconnect-intent")
        mark = len(self.alice.events)
        self.active_protocol("alice").close(error_code=0x1100, reason_phrase="test reconnect")
        await self.alice.wait("connection", lambda item: item["state"] == "connected", since=mark, timeout=8)
        await self.alice.wait("message-sends", lambda item: not item["items"], since=mark)
        self.assert_single_message_intent("reconnect-intent", 2)

    async def test_expired_session_reauthenticates_and_preserves_message_intent(self):
        old_session = self.active_protocol("alice").m_session_id
        self.auth.m_session_mgr.m_sessions.pop(old_session)
        mark = await self.alice.command(
            "message", conversation=self.conversation, intent="expired-session", text="after reauthentication")
        welcome = await self.alice.wait("connection", lambda item: item["state"] == "connected", since=mark, timeout=8)
        self.assertNotEqual(old_session, welcome["sessionId"])
        await self.alice.wait("message-sends", lambda item: not item["items"], since=mark)
        self.assert_single_message_intent("expired-session", 1)
        self.assertEqual(self.session_rejections, [self.message_attempts[-1]["requestId"]])

    async def test_manual_disconnect_cancels_scheduled_reconnect_and_user_switch(self):
        self.rejected_message_intents["pending-alice"] = 503
        await self.alice.command("message", conversation=self.conversation, intent="pending-alice", text="private")
        await self.alice.wait("message-sends", lambda item: any(x["code"] == 503 for x in item["items"]))
        mark = len(self.alice.events)
        self.active_protocol("alice").close(error_code=0x1100, reason_phrase="cancel retry")
        await self.alice.wait("connection", lambda item: item["state"] == "reconnecting", since=mark, timeout=5)
        await self.disconnect(self.alice)
        connected_before = len(self.protocols)
        mark = len(self.alice.events)
        await asyncio.sleep(1.3)
        self.assertEqual(connected_before, len(self.protocols))
        self.assertFalse(any(event["event"] == "connection" and event["data"]["state"] == "connected"
                             for event in self.alice.events[mark:]))
        initial = await self.alice.connect(self.endpoint, "carol")
        self.assertEqual([], initial["messageSends"])
        await asyncio.sleep(1.3)
        self.assertEqual(["alice"], [attempt["user"] for attempt in self.message_attempts])
        await self.disconnect(self.alice)
        self.rejected_message_intents.clear()
        await self.alice.connect(self.endpoint, "alice")
        await self.alice.wait("message-sends", lambda item: not item["items"], since=mark)
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "pending-alice")
        self.assert_single_message_intent("pending-alice", 2)

    async def test_fragmented_coalesced_control_events_apply_once_in_order(self):
        await self.synced(self.alice)
        protocol = self.active_protocol("alice")
        frames = []
        intents = ["split-first", "split-second"]
        mark = len(self.alice.events)
        with patch.object(protocol, "_send", lambda stream, envelope: frames.append(EnvelopeCodec.encode_frame(envelope))):
            for intent in intents:
                result = protocol.m_message_service.handle_send_message("bob", intent, message_pb2.SendMessage(
                    conversation_id=self.conversation, client_msg_id=intent, type=common_pb2.MSG_TEXT,
                    content=("fragmented " + intent).encode()))
                self.assertTrue(result.ack.success)
                for event in result.sync_events:
                    if event.user_id == "alice":
                        protocol.send_sync_event(event)
        self.assertEqual(2, len(frames))
        wire = b"".join(frames)
        begin = 0
        for end in (1, 2, 3, 4, 7, len(frames[0]) - 1, len(wire)):
            protocol._quic.send_stream_data(protocol.m_control_stream_id, wire[begin:end])
            protocol.transmit()
            await asyncio.sleep(0.02)
            begin = end
        await self.alice.wait("message", lambda item: item["clientMsgId"] == intents[-1], since=mark)
        observed = [item["data"]["clientMsgId"] for item in self.alice.events[mark:]
            if item["event"] == "message" and item["data"]["clientMsgId"] in intents]
        self.assertEqual(intents, observed)
        self.assertFalse(any(item["event"] == "connection" and item["data"]["state"] == "reconnecting"
            for item in self.alice.events[mark:]))

    async def check_invalid_control_frame_recovery(self, data, intent, end_stream=False):
        self.rejected_message_intents[intent] = 503
        mark = await self.alice.command("message", conversation=self.conversation, intent=intent, text="preserve intent")
        await self.alice.wait("message-sends", lambda item: any(x["code"] == 503 for x in item["items"]), since=mark)
        self.rejected_message_intents.clear()
        protocol = self.active_protocol("alice")
        mark = len(self.alice.events)
        protocol._quic.send_stream_data(protocol.m_control_stream_id, data, end_stream=end_stream)
        protocol.transmit()
        await self.alice.wait("connection", lambda item: item["state"] == "reconnecting", since=mark, timeout=3)
        await self.alice.wait("connection", lambda item: item["state"] == "connected", since=mark, timeout=8)
        self.assertIsNot(protocol, self.active_protocol("alice"))
        await self.alice.wait("message-sends", lambda item: not item["items"], since=mark)
        await self.bob.wait("message", lambda item: item["clientMsgId"] == intent)
        self.assert_single_message_intent(intent, 2)

    async def test_empty_control_frame_reconnects_with_original_message(self):
        await self.check_invalid_control_frame_recovery(b"\x00\x00\x00\x00", "empty-frame")

    async def test_oversized_control_frame_reconnects_with_original_message(self):
        await self.check_invalid_control_frame_recovery((16 * 1024 * 1024 + 1).to_bytes(4, "big"), "large-frame")

    async def test_malformed_control_payload_reconnects_with_original_message(self):
        await self.check_invalid_control_frame_recovery(b"\x00\x00\x00\x01\xff", "bad-payload")

    async def test_truncated_control_stream_reconnects_with_original_message(self):
        await self.check_invalid_control_frame_recovery(b"\x00\x00\x00\x20\x08", "truncated-frame", True)

    async def test_control_stream_reset_reconnects_before_next_message(self):
        protocol = self.active_protocol("alice")
        mark = len(self.alice.events)
        protocol._quic.reset_stream(protocol.m_control_stream_id, error_code=0x1101)
        protocol._quic.stop_stream(protocol.m_control_stream_id, error_code=0x1101)
        protocol.transmit()
        await self.alice.wait("connection", lambda item: item["state"] == "connected", since=mark, timeout=8)
        await self.alice.command("message", conversation=self.conversation, intent="after-reset", text="control restored")
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "after-reset")

    async def test_missing_application_responses_trigger_heartbeat_recovery(self):
        await self.disconnect(self.bob)
        self.heartbeat_interval = 1
        await self.bob.connect(self.endpoint, "bob")
        await self.synced(self.bob)
        mark = len(self.bob.events)
        self.silent_user = "bob"
        await self.bob.wait("error", lambda item: "server response timed out" in item["message"],
                            since=mark, timeout=5)
        self.silent_user = ""
        await self.bob.wait("connection", lambda item: item["state"] == "connected", since=mark, timeout=8)
        await self.alice.command("message", conversation=self.conversation, intent="after-silence", text="responsive")
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "after-silence")

    async def test_missing_welcome_times_out_and_reconnects(self):
        await self.disconnect(self.bob)
        self.drop_welcome_count = 1
        mark = await self.bob.command("connect", endpoint=self.endpoint, token="dev-token:bob", device="device-bob")
        await self.bob.wait("error", lambda item: "login timed out" in item["message"], since=mark, timeout=13)
        initial = await self.bob.wait("initial", since=mark, timeout=8)
        self.assertEqual("bob", initial["currentUser"]["userId"])

    async def test_download_with_delayed_control_metadata(self):
        payload = bytes(range(256)) * 4096 + b"non-aligned-tail!"
        file_id = await self.upload(payload)
        self.delay_metadata = True
        target = self.root / "download.bin"
        target.write_bytes(b"previous content")
        mark = await self.bob.command("download", conversation=self.conversation, source=file_id, path=str(target))
        complete = await self.bob.wait("file", lambda item: item["completed"] and item["fileId"] != file_id, since=mark)
        self.assertEqual(payload, target.read_bytes())
        transfer = self.files.get_transfer_by_file_id(complete["fileId"])
        self.assertEqual("completed", transfer.status)
        self.assertEqual(len(payload), transfer.received_bytes)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), transfer.sha256)
        self.assertFalse(list(target.parent.glob(target.name + ".miniim-*.part")))
        self.assertEqual(1, self.db.execute_fetchone(
            "SELECT COUNT(*) FROM messages WHERE type = ?", (common_pb2.MSG_FILE,),
        )[0])

    async def test_stalled_download_keeps_control_responsive_and_buffers_bounded(self):
        payload = bytes(range(256)) * 8192
        file_id = await self.upload(payload)
        self.delay_metadata = True
        self.hold_metadata = True
        target = self.root / "stalled.bin"
        mark = await self.bob.command("download", conversation=self.conversation, source=file_id, path=str(target))
        bob_protocol = next(protocol for protocol in self.protocols if protocol.m_user_id == "bob")
        async with asyncio.timeout(5):
            while not self.deferred_metadata or not bob_protocol.m_download_sender.jobs:
                await asyncio.sleep(0.001)
        await self.alice.command("message", conversation=self.conversation, intent="during-download", text="responsive")
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "during-download", since=mark)
        from quic.download import STREAM_BUFFER_LIMIT
        self.assertLessEqual(bob_protocol.m_download_sender.peak_pending_bytes, STREAM_BUFFER_LIMIT)
        self.assertFalse(target.exists())
        self.assertTrue(all(not job.eof for job in bob_protocol.m_download_sender.jobs.values()))
        for send_metadata in self.deferred_metadata:
            send_metadata()
        self.deferred_metadata.clear()
        await self.bob.wait("file", lambda item: item["completed"] and item["fileId"] != file_id, since=mark)
        self.assertEqual(payload, target.read_bytes())

    async def test_disconnect_with_deferred_download_allows_next_connection(self):
        payload = bytes(range(256)) * 1024
        file_id = await self.upload(payload)
        self.delay_metadata = True
        self.hold_metadata = True
        target = self.root / "deferred.bin"
        target.write_bytes(b"existing")
        mark = await self.bob.command("download", conversation=self.conversation, source=file_id, path=str(target))
        async with asyncio.timeout(5):
            while not self.deferred_metadata:
                await asyncio.sleep(0.001)
        # This control round trip proves Qt has continued processing while the file receive waits.
        await self.alice.command("message", conversation=self.conversation, intent="before-disconnect", text="barrier")
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "before-disconnect", since=mark)
        mark = await self.bob.command("disconnect")
        await self.bob.wait("connection", lambda item: item["state"] == "disconnected", since=mark)
        self.assertEqual(b"existing", target.read_bytes())
        self.assertFalse(self.db.execute_fetchone(
            "SELECT 1 FROM file_transfers WHERE direction = ? AND status = 'completed'",
            (common_pb2.FILE_DIRECTION_DOWNLOAD,),
        ))
        self.deferred_metadata.clear()
        self.hold_metadata = False
        self.delay_metadata = False
        mark = len(self.bob.events)
        initial = await self.bob.connect(self.endpoint, "bob")
        self.assertEqual(1, len(initial["fileTasks"]))
        await self.bob.wait("file", lambda item: item["completed"] and item["fileId"] != file_id, since=mark)
        self.assertEqual(payload, target.read_bytes())
        self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM file_transfers WHERE direction=2")[0])

    async def test_normal_exit_with_deferred_download_restores_original_task(self):
        payload = bytes(range(251)) * 4096
        file_id = await self.upload(payload)
        self.delay_metadata = True
        self.hold_metadata = True
        target = self.root / "exit-deferred.bin"
        target.write_bytes(b"preserve until verified")
        mark = await self.bob.command("download", conversation=self.conversation, source=file_id, path=str(target))
        async with asyncio.timeout(5):
            while not self.deferred_metadata:
                await asyncio.sleep(0.001)
        await self.alice.command("message", conversation=self.conversation, intent="before-exit", text="barrier")
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "before-exit", since=mark)
        # Closing the controller exits the Qt event loop, then destroys the still-connected bridge.
        # close() requires exit code 0; a destructor deadlock is killed after 5 seconds and fails.
        await self.bob.close()
        self.assertEqual(b"preserve until verified", target.read_bytes())
        self.assertFalse(self.db.execute_fetchone(
            "SELECT 1 FROM file_transfers WHERE direction=2 AND status='completed'"))
        self.deferred_metadata.clear()
        self.hold_metadata = False
        self.delay_metadata = False
        initial = await self.restart_bob()
        self.assertEqual(1, len(initial["fileTasks"]))
        await self.bob.wait("file-tasks", lambda item: not item["items"])
        self.assert_single_file_intent("bob", 2)
        self.assertEqual(payload, target.read_bytes())

    async def test_normal_exit_while_waiting_for_welcome(self):
        await self.disconnect(self.bob)
        self.drop_welcome_count = 1
        mark = await self.bob.command(
            "connect", endpoint=self.endpoint, token="dev-token:bob", device="device-bob")
        async with asyncio.timeout(5):
            while self.drop_welcome_count:
                await asyncio.sleep(0.001)
        self.assertFalse(any(item["event"] == "initial" for item in self.bob.events[mark:]))
        await self.bob.close()
        initial = await self.restart_bob()
        self.assertEqual("bob", initial["currentUser"]["userId"])
        await self.alice.command("message", conversation=self.conversation, intent="after-exit", text="restored")
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "after-exit")

    async def test_concurrent_downloads_preserve_distinct_targets(self):
        payload = bytes(range(251)) * 128
        file_id = await self.upload(payload)
        self.delay_metadata = True
        mark = len(self.bob.events)
        targets = [self.root / f"parallel-{index}.bin" for index in range(3)]
        for target in targets:
            target.write_bytes(b"previous")
            await self.bob.command("download", conversation=self.conversation, source=file_id, path=str(target))
        completed_ids = set()
        for _ in targets:
            completed = await self.bob.wait(
                "file", lambda item: item["completed"]
                and item["fileId"] != file_id and item["fileId"] not in completed_ids, since=mark,
            )
            completed_ids.add(completed["fileId"])
        self.assertEqual(3, len(completed_ids))
        for target in targets:
            self.assertEqual(payload, target.read_bytes())
        self.assertEqual(3, self.db.execute_fetchone(
            "SELECT COUNT(*) FROM file_transfers WHERE direction = ? AND status = 'completed'",
            (common_pb2.FILE_DIRECTION_DOWNLOAD,),
        )[0])

    async def test_truncated_and_corrupted_downloads_preserve_destination(self):
        payload = b"verified native download" * 4096
        file_id = await self.upload(payload)
        stored = self.files.get_storage_path(file_id)
        for content, error in ((payload[:-1], "source file unavailable"), (b"x" * len(payload), "sha256 mismatch")):
            with self.subTest(error=error):
                stored.write_bytes(content)
                target = self.root / "protected.bin"
                target.write_bytes(b"previous content")
                mark = await self.bob.command("download", conversation=self.conversation, source=file_id, path=str(target))
                await self.bob.wait("error", lambda item: error in item["message"], since=mark)
                self.assertEqual(b"previous content", target.read_bytes())
                rows = self.db.execute_fetchall("SELECT status FROM file_transfers WHERE direction = ?", (common_pb2.FILE_DIRECTION_DOWNLOAD,))
                if error == "sha256 mismatch":
                    self.assertTrue(rows)
                self.assertTrue(all(row["status"] != "completed" for row in rows))
        stored.write_bytes(payload)
        target = self.root / "recovered.bin"
        mark = await self.bob.command("download", conversation=self.conversation, source=file_id, path=str(target))
        await self.bob.wait("file", lambda item: item["completed"] and item["fileId"] != file_id, since=mark)
        self.assertEqual(payload, target.read_bytes())

    def assert_single_file_intent(self, user, direction):
        rows = self.db.execute_fetchall(
            "SELECT * FROM file_transfers WHERE owner_id=? AND direction=?", (user, direction))
        self.assertEqual(1, len(rows))
        row = rows[0]
        attempts = [item for item in self.file_attempts if item["intent"] == row["client_file_id"]]
        self.assertGreaterEqual(len(attempts), 2)
        self.assertEqual(1, len({item["requestId"] for item in attempts}))
        self.assertEqual({row["file_id"]}, {item["fileId"] for item in attempts})
        self.assertEqual("completed", row["status"])
        return row, attempts

    async def test_upload_restart_resumes_original_task_and_offset(self):
        self.fault.file_drop_after_bytes = 262144
        payload = bytes(range(251)) * 8192
        source = self.root / "restart-upload.bin"
        source.write_bytes(payload)
        mark = await self.alice.command("upload", conversation=self.conversation, path=str(source))
        await self.alice.wait("connection", lambda item: item["state"] == "disconnected", since=mark)
        row = self.db.execute_fetchone("SELECT * FROM file_transfers WHERE direction=1")
        self.assertGreater(row["received_bytes"], 0)
        self.assertLess(row["received_bytes"], len(payload))
        await self.alice.crash()
        self.fault.file_drop_after_bytes = 0
        initial = await self.restart_alice()
        self.assertEqual(row["client_file_id"], initial["fileTasks"][0]["clientFileId"])
        await self.alice.wait("file-tasks", lambda item: not item["items"])
        row, _ = self.assert_single_file_intent("alice", 1)
        self.assertEqual(payload, self.files.get_storage_path(row["file_id"]).read_bytes())
        self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])

    async def _resume_upload_after_storage_damage(self, damage):
        self.fault.file_drop_after_bytes = 262144
        payload = bytes(range(251)) * 8192
        source = self.root / "storage-recovery.bin"
        source.write_bytes(payload)
        mark = await self.alice.command("upload", conversation=self.conversation, path=str(source))
        await self.alice.wait("connection", lambda item: item["state"] == "disconnected", since=mark)
        await self.alice.crash()
        row = self.db.execute_fetchone("SELECT * FROM file_transfers WHERE direction=1")
        self.assertGreater(row["received_bytes"], 0)
        path = self.files.get_storage_path(row["file_id"])
        if damage == "missing":
            path.unlink()
            offset = 0
        elif damage == "short":
            offset = row["received_bytes"] // 2
            path.write_bytes(payload[:offset])
        else:
            offset = row["received_bytes"]
            with path.open("ab") as file:
                file.write(b"uncommitted bytes" * 7)
        self.fault.file_drop_after_bytes = 0
        initial = await self.restart_alice()
        self.assertEqual(row["client_file_id"], initial["fileTasks"][0]["clientFileId"])
        started = time.monotonic()
        await self.alice.wait("file-tasks", lambda item: not item["items"], timeout=60)
        (self.output_dir / (self._testMethodName + "-recovery-timing.json")).write_text(json.dumps(dict(
            bytes=len(payload), completionWaitSeconds=time.monotonic() - started, timeoutSeconds=60,
            scope="functional recovery wait after client restart; not isolated throughput")), encoding="utf-8")
        restored, attempts = self.assert_single_file_intent("alice", 1)
        self.assertEqual(offset, attempts[1]["acceptedOffset"])
        self.assertEqual(payload, self.files.get_storage_path(restored["file_id"]).read_bytes())
        self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])

    async def test_upload_restart_recovers_truncated_server_file(self):
        await self._resume_upload_after_storage_damage("short")

    async def test_upload_restart_recovers_missing_server_file(self):
        await self._resume_upload_after_storage_damage("missing")

    async def test_upload_restart_discards_uncommitted_server_tail(self):
        await self._resume_upload_after_storage_damage("tail")

    async def _retry_upload_after_storage_error(self, fault):
        self.upload_storage_fault = fault
        payload = bytes(range(251)) * 8192
        source = self.root / "storage-error.bin"
        source.write_bytes(payload)
        mark = await self.alice.command("upload", conversation=self.conversation, path=str(source))
        await self.alice.wait("connection", lambda item: item["state"] == "disconnected", since=mark)
        self.assertEqual("", self.upload_storage_fault)
        started = time.monotonic()
        await self.alice.wait("file-tasks", lambda item: not item["items"], since=mark, timeout=60)
        (self.output_dir / (self._testMethodName + "-recovery-timing.json")).write_text(json.dumps(dict(
            bytes=len(payload), completionWaitSeconds=time.monotonic() - started, timeoutSeconds=60,
            scope="functional recovery wait after disconnect; not isolated throughput")), encoding="utf-8")
        row, attempts = self.assert_single_file_intent("alice", 1)
        self.assertEqual(0, attempts[1]["acceptedOffset"])
        self.assertEqual(payload, self.files.get_storage_path(row["file_id"]).read_bytes())
        self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])

    async def test_upload_reconnects_after_server_sync_failure(self):
        await self._retry_upload_after_storage_error("sync")

    async def test_upload_reconnects_after_server_progress_commit_failure(self):
        await self._retry_upload_after_storage_error("database")

    async def test_upload_finish_restart_rechecks_server_progress(self):
        self.hold_upload_finish = True
        payload = bytes(range(251)) * 512
        source = self.root / "finish-recovery.bin"
        source.write_bytes(payload)
        mark = await self.alice.command("upload", conversation=self.conversation, path=str(source))
        await self.alice.wait("file-tasks", lambda item: any(task["status"] == "finishing"
            for task in item["items"]), since=mark)
        await self.alice.crash()
        row = self.db.execute_fetchone("SELECT * FROM file_transfers WHERE direction=1")
        self.files.get_storage_path(row["file_id"]).write_bytes(payload[:1024])
        self.hold_upload_finish = False
        initial = await self.restart_alice()
        self.assertEqual(row["client_file_id"], initial["fileTasks"][0]["clientFileId"])
        await self.alice.wait("file-tasks", lambda item: not item["items"], timeout=8)
        recovered, attempts = self.assert_single_file_intent("alice", 1)
        self.assertEqual(1024, attempts[1]["acceptedOffset"])
        self.assertEqual(payload, self.files.get_storage_path(recovered["file_id"]).read_bytes())
        self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])

    async def test_upload_digest_failure_retries_original_intent(self):
        self.fault.file_drop_after_bytes = 65536
        payload = bytes(range(251)) * 512
        source = self.root / "corrupt-upload.bin"
        source.write_bytes(payload)
        mark = await self.alice.command("upload", conversation=self.conversation, path=str(source))
        await self.alice.wait("connection", lambda item: item["state"] == "disconnected", since=mark)
        await self.alice.crash()
        row = self.db.execute_fetchone("SELECT * FROM file_transfers WHERE direction=1")
        self.assertGreater(row["received_bytes"], 0)
        stored = self.files.get_storage_path(row["file_id"])
        damaged = bytearray(stored.read_bytes())
        damaged[0] ^= 0xff
        stored.write_bytes(damaged)
        self.fault.file_drop_after_bytes = 0
        await self.restart_alice()
        await self.alice.wait("file-tasks", lambda item: any(task["status"] == "failed"
            and "sha256 mismatch" in task["error"] for task in item["items"]))
        self.assertEqual("failed_integrity", self.files.get_transfer_by_file_id(row["file_id"]).status)
        self.assertEqual(0, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])
        failed_request = self.db.execute_fetchone(
            "SELECT request_id,ack FROM control_write_results WHERE operation='file_finish' AND user_id='alice'")
        self.assertIsNotNone(failed_request)
        await self.alice.crash()
        initial = await self.restart_alice()
        self.assertTrue(initial["fileTasks"][0]["finishRejected"])
        self.assertEqual(failed_request["request_id"], initial["fileTasks"][0]["finishRequestId"])
        mark = await self.alice.command("retry-file", intent=row["client_file_id"])
        await self.alice.wait("file-tasks", lambda item: not item["items"], since=mark)
        results = self.db.execute_fetchall(
            "SELECT request_id,ack FROM control_write_results WHERE operation='file_finish' AND user_id='alice'")
        self.assertEqual(2, len(results))
        self.assertEqual({0, 409}, {message_pb2.Ack.FromString(item["ack"]).code for item in results})
        self.assertEqual(failed_request["ack"], next(item["ack"] for item in results
            if item["request_id"] == failed_request["request_id"]))
        recovered, attempts = self.assert_single_file_intent("alice", 1)
        self.assertEqual(0, attempts[-1]["acceptedOffset"])
        self.assertEqual(payload, stored.read_bytes())
        self.assertEqual(row["file_id"], recovered["file_id"])
        self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])

    async def test_download_restart_uses_existing_partial_file(self):
        payload = bytes(range(251)) * 4096
        file_id = await self.upload(payload)
        self.pause_download_at = 65536
        target = self.root / "resume-download.bin"
        target.write_bytes(b"keep until verified")
        await self.bob.command("download", conversation=self.conversation, source=file_id, path=str(target))
        async with asyncio.timeout(5):
            while True:
                parts = list(self.root.glob("resume-download.bin.miniim-*.part"))
                if parts and parts[0].stat().st_size >= self.pause_download_at:
                    break
                await asyncio.sleep(0.01)
        partial = parts[0].stat().st_size
        self.assertEqual(b"keep until verified", target.read_bytes())
        await self.bob.crash()
        self.pause_download_at = 0
        initial = await self.restart_bob()
        self.assertEqual(1, len(initial["fileTasks"]))
        await self.bob.wait("file-tasks", lambda item: not item["items"])
        row, attempts = self.assert_single_file_intent("bob", 2)
        self.assertEqual(partial, attempts[-1]["offset"])
        self.assertEqual(payload, target.read_bytes())

    async def test_download_published_before_lost_finish_ack_survives_restart(self):
        payload = b"published download" * 8192
        file_id = await self.upload(payload)
        self.drop_file_acks["filefinish"] = 1
        target = self.root / "published.bin"
        mark = await self.bob.command("download", conversation=self.conversation, source=file_id, path=str(target))
        await self.bob.wait("file", lambda item: item["completed"] and item["fileId"] != file_id, since=mark)
        self.assertEqual(payload, target.read_bytes())
        await self.bob.crash()
        initial = await self.restart_bob()
        self.assertEqual("finishing", initial["fileTasks"][0]["status"])
        await self.bob.wait("file-tasks", lambda item: not item["items"])
        self.assertEqual(payload, target.read_bytes())
        self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM file_transfers WHERE direction=2")[0])

    async def test_file_finish_retry_survives_message_store_failure(self):
        payload = b"file confirmation stays independent" * 2048
        file_id = await self.upload(payload)
        self.drop_file_acks["filefinish"] = 1
        target = self.root / "independent-finish.bin"
        mark = await self.bob.command("download", conversation=self.conversation, source=file_id, path=str(target))
        await self.bob.wait("file", lambda item: item["completed"] and item["fileId"] != file_id, since=mark)
        self.assertEqual(payload, target.read_bytes())
        cache = next((self.root / "state-bob").glob("*.sqlite"))
        with closing(sqlite3.connect(cache)) as native_db:
            native_db.execute("CREATE TRIGGER reject_message_attempt BEFORE UPDATE OF attempts ON message_outbox "
                              "BEGIN SELECT RAISE(ABORT,'injected message attempt failure'); END")
            native_db.commit()
            try:
                mark = await self.bob.command("message", conversation=self.conversation,
                                              intent="stored-but-not-sent", text="saved")
                await self.bob.wait("error", lambda item: "injected message attempt failure" in item["message"], since=mark)
                await self.bob.wait("file-tasks", lambda item: not item["items"], since=mark, timeout=8)
                self.assertEqual("completed", native_db.execute("SELECT status FROM file_tasks").fetchone()[0])
                self.assertEqual(("pending", 0), native_db.execute(
                    "SELECT status,attempts FROM message_outbox WHERE client_msg_id='stored-but-not-sent'").fetchone())
                self.assertEqual(payload, target.read_bytes())
            finally:
                native_db.execute("DROP TRIGGER reject_message_attempt")
                native_db.commit()

    async def test_upload_init_ack_loss_reuses_existing_intent(self):
        self.drop_file_acks["fileinit"] = 1
        source = self.root / "init-ack.bin"
        source.write_bytes(b"same intent" * 8192)
        mark = await self.alice.command("upload", conversation=self.conversation, path=str(source))
        await self.alice.wait("file", lambda item: item["completed"], since=mark, timeout=12)
        self.assert_single_file_intent("alice", 1)

    async def test_changed_upload_is_rejected_and_explicit_retry_keeps_intent(self):
        self.fault.file_drop_after_bytes = 262144
        source = self.root / "changed-source.bin"
        payload = b"original source" * 65536
        source.write_bytes(payload)
        mark = await self.alice.command("upload", conversation=self.conversation, path=str(source))
        await self.alice.wait("connection", lambda item: item["state"] == "disconnected", since=mark)
        await self.alice.crash()
        source.write_bytes(b"x" * len(payload))
        self.fault.file_drop_after_bytes = 0
        initial = await self.restart_alice()
        intent = initial["fileTasks"][0]["clientFileId"]
        await self.alice.wait("file-tasks", lambda item: any(x["status"] == "failed" for x in item["items"]))
        self.assertEqual(0, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])
        source.write_bytes(payload)
        mark = await self.alice.command("retry-file", intent=intent)
        await self.alice.wait("file", lambda item: item["completed"], since=mark)
        self.assert_single_file_intent("alice", 1)

    async def test_cancelled_download_stays_cancelled_after_restart(self):
        payload = b"cancelled body" * 65536
        file_id = await self.upload(payload)
        self.pause_download_at = 65536
        target = self.root / "cancelled.bin"
        target.write_bytes(b"keep")
        mark = await self.bob.command("download", conversation=self.conversation, source=file_id, path=str(target))
        task = await self.bob.wait("file-tasks", lambda item: bool(item["items"]), since=mark)
        async with asyncio.timeout(5):
            while not any(protocol.m_user_id == "bob" and any(job.offset >= 65536
                    for job in protocol.m_download_sender.jobs.values()) for protocol in self.protocols):
                await asyncio.sleep(0.01)
        async with asyncio.timeout(5):
            while not any(path.stat().st_size >= 65536 for path in self.root.glob("cancelled.bin.miniim-*.part")):
                await asyncio.sleep(0.01)
        mark = await self.bob.command("cancel-file", intent=task["items"][0]["clientFileId"])
        await self.bob.wait("file-tasks", lambda item: not item["items"], since=mark)
        await self.bob.crash()
        self.pause_download_at = 0
        attempts = len(self.file_attempts)
        initial = await self.restart_bob()
        self.assertEqual([], initial["fileTasks"])
        await self.synced(self.bob)
        self.assertEqual(attempts, len(self.file_attempts))
        self.assertEqual(b"keep", target.read_bytes())
        rows = self.db.execute_fetchall("SELECT * FROM file_transfers WHERE owner_id='bob'")
        cancelled = self.db.execute_fetchone("SELECT * FROM file_cancellations WHERE owner_id='bob'")
        self.assertIsNotNone(cancelled)
        self.assertTrue(all(row["status"] == "cancelled" for row in rows))
        part = next(self.root.glob("cancelled.bin.miniim-*.part"))
        original = part.read_bytes()
        mark = await self.bob.command("preview-file-cleanup")
        preview = await self.bob.wait("file-cleanup", since=mark)
        self.assertTrue(preview["ok"])
        self.assertEqual(1, len(preview["items"]))
        self.assertEqual(len(original), preview["items"][0]["bytes"])
        self.assertEqual(original, part.read_bytes())
        mark = await self.bob.command("disconnect")
        await self.bob.wait("connection", lambda item: item["state"] == "disconnected", since=mark)
        await self.bob.connect(self.endpoint, "alice")
        mark = await self.bob.command("apply-file-cleanup", token=preview["token"])
        self.assertFalse((await self.bob.wait("file-cleanup", since=mark))["ok"])
        self.assertTrue(part.exists())
        mark = await self.bob.command("disconnect")
        await self.bob.wait("connection", lambda item: item["state"] == "disconnected", since=mark)
        await self.bob.connect(self.endpoint, "bob")
        mark = await self.bob.command("preview-file-cleanup")
        preview = await self.bob.wait("file-cleanup", since=mark)
        mark = await self.bob.command("apply-file-cleanup", token=preview["token"])
        removed = await self.bob.wait("file-cleanup", since=mark)
        self.assertTrue(removed["ok"])
        self.assertEqual(1, removed["removed"])
        self.assertFalse(part.exists())
        self.assertEqual(b"keep", target.read_bytes())
        self.assertEqual([dict(row) for row in rows], [dict(row) for row in self.db.execute_fetchall(
            "SELECT * FROM file_transfers WHERE owner_id='bob'")])
        await self.bob.crash()
        self.assertEqual([], (await self.restart_bob())["fileTasks"])
        mark = await self.bob.command("preview-file-cleanup")
        self.assertEqual([], (await self.bob.wait("file-cleanup", since=mark))["items"])
        self.assertEqual(attempts, len(self.file_attempts))
        self.assertEqual(dict(cancelled), dict(self.db.execute_fetchone("SELECT * FROM file_cancellations WHERE owner_id='bob'")))

    async def _pending_uninitialized_upload(self):
        self.block_file_init = True
        source = self.root / "cancel-source.bin"
        source.write_bytes(b"cancel before initialization" * 4096)
        mark = await self.alice.command("upload", conversation=self.conversation, path=str(source))
        tasks = await self.alice.wait("file-tasks", lambda item: bool(item["items"]), since=mark)
        return tasks["items"][0]["clientFileId"]

    async def _wait_cancel_attempts(self, count):
        async with asyncio.timeout(10):
            while len(self.cancel_attempts) < count:
                await asyncio.sleep(0.01)

    async def test_cancel_before_initialization_survives_restart_and_ack_loss(self):
        intent = await self._pending_uninitialized_upload()
        self.reject_file_cancel = 503
        mark = await self.alice.command("cancel-file", intent=intent)
        await self.alice.wait("file-tasks", lambda item: any(task["status"] == "cancelling"
            and task["error"] for task in item["items"]), since=mark)
        await self.alice.crash()
        original = self.cancel_attempts[0]
        self.reject_file_cancel = 0
        self.drop_file_acks["filecancel"] = 1
        self.block_file_init = False
        initial = await self.restart_alice()
        self.assertEqual("cancelling", initial["fileTasks"][0]["status"])
        await self.alice.wait("file-tasks", lambda item: not item["items"])
        self.assertGreaterEqual(len(self.cancel_attempts), 3)
        self.assertTrue(all(item == original for item in self.cancel_attempts))
        self.assertEqual(0, self.db.execute_fetchone("SELECT COUNT(*) FROM file_transfers")[0])
        self.assertIsNotNone(self.db.execute_fetchone("SELECT 1 FROM file_cancellations WHERE owner_id='alice' AND client_file_id=?", (intent,)))
        await self.alice.crash()
        self.assertEqual([], (await self.restart_alice())["fileTasks"])
        self.assertEqual(0, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])

    async def test_cancel_save_failure_does_not_stop_or_change_original_task(self):
        intent = await self._pending_uninitialized_upload()
        cache = next((self.root / "state-alice").glob("*.sqlite"))
        with closing(sqlite3.connect(cache)) as connection, connection:
            connection.execute("CREATE TRIGGER reject_cancel BEFORE UPDATE ON file_tasks "
                "WHEN NEW.status='cancelling' BEGIN SELECT RAISE(ABORT,'cancel save failure'); END")
        with self.assertRaisesRegex(AssertionError, "native command rejected"):
            await self.alice.command("cancel-file", intent=intent)
        self.assertEqual([], self.cancel_attempts)
        with closing(sqlite3.connect(cache)) as connection, connection:
            self.assertEqual("pending", connection.execute("SELECT status FROM file_tasks").fetchone()[0])
            connection.execute("DROP TRIGGER reject_cancel")
        mark = await self.alice.command("cancel-file", intent=intent)
        await self.alice.wait("file-tasks", lambda item: not item["items"], since=mark)

    async def test_failed_cancellation_stays_stopped_and_new_cancel_uses_new_request(self):
        intent = await self._pending_uninitialized_upload()
        self.reject_file_cancel = 403
        mark = await self.alice.command("cancel-file", intent=intent)
        await self.alice.wait("file-tasks", lambda item: any(task["status"] == "cancel_failed"
            for task in item["items"]), since=mark)
        old = self.cancel_attempts[0]["requestId"]
        await self.alice.crash()
        initial = await self.restart_alice()
        self.assertEqual("cancel_failed", initial["fileTasks"][0]["status"])
        self.assertEqual(1, len(self.cancel_attempts))
        self.assertEqual(0, self.db.execute_fetchone("SELECT COUNT(*) FROM file_transfers")[0])
        self.reject_file_cancel = 0
        mark = await self.alice.command("cancel-file", intent=intent)
        await self.alice.wait("file-tasks", lambda item: not item["items"], since=mark)
        self.assertNotEqual(old, self.cancel_attempts[-1]["requestId"])
        self.assertEqual(intent, self.cancel_attempts[-1]["intent"])

    async def test_pending_file_cancel_isolated_across_account_switch(self):
        intent = await self._pending_uninitialized_upload()
        self.reject_file_cancel = 503
        mark = await self.alice.command("cancel-file", intent=intent)
        await self.alice.wait("file-tasks", lambda item: any(task["status"] == "cancelling"
            and task["error"] for task in item["items"]), since=mark)
        old = self.cancel_attempts[0]
        mark = await self.alice.command("disconnect")
        await self.alice.wait("connection", lambda item: item["state"] == "disconnected", since=mark)
        other = await self.alice.connect(self.endpoint, "bob")
        self.assertEqual([], other["fileTasks"])
        self.reject_file_cancel = 0
        mark = await self.alice.command("disconnect")
        await self.alice.wait("connection", lambda item: item["state"] == "disconnected", since=mark)
        await self.alice.connect(self.endpoint, "alice")
        await self.alice.wait("file-tasks", lambda item: not item["items"], since=mark)
        self.assertTrue(all(item == old for item in self.cancel_attempts))

    async def test_missing_published_download_retries_original_task_after_restart(self):
        payload = b"recover published bytes" * 8192
        file_id = await self.upload(payload)
        source = self.files.get_storage_path(file_id)
        publication = self.files.m_file_repo.get_transfer_by_file_id(file_id)
        source.unlink()
        target = self.root / "missing-source.bin"
        target.write_bytes(b"existing")
        mark = await self.bob.command("download", conversation=self.conversation, source=file_id, path=str(target))
        failed = await self.bob.wait("file-tasks",
            lambda item: any(task["status"] == "failed" for task in item["items"]), since=mark)
        task = next(task for task in failed["items"] if task["status"] == "failed")
        self.assertEqual(b"existing", target.read_bytes())
        self.assertIn("source file unavailable", task["error"])
        await self.bob.crash()
        initial = await self.restart_bob()
        recovered = initial["fileTasks"][0]
        for key in ("clientFileId", "requestId", "finishRequestId", "status"):
            self.assertEqual(task[key], recovered[key])
        source.write_bytes(payload)
        mark = await self.bob.command("retry-file", intent=task["clientFileId"])
        await self.bob.wait("file-tasks", lambda item: not item["items"], since=mark)
        self.assertEqual(payload, target.read_bytes())
        attempts = [item for item in self.file_attempts if item["user"] == "bob"]
        self.assertGreaterEqual(len(attempts), 2)
        self.assertEqual({task["requestId"]}, {item["requestId"] for item in attempts})
        self.assertEqual({task["clientFileId"]}, {item["intent"] for item in attempts})
        self.assertEqual(2, self.db.execute_fetchone("SELECT COUNT(*) FROM file_transfers")[0])
        self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])
        self.assertEqual(publication, self.files.m_file_repo.get_transfer_by_file_id(file_id))

    async def test_published_source_truncated_during_download_can_retry(self):
        payload = bytes(range(251)) * 4096
        file_id = await self.upload(payload)
        source = self.files.get_storage_path(file_id)
        self.pause_download_at = 65536
        target = self.root / "truncated-source.bin"
        target.write_bytes(b"existing")
        mark = await self.bob.command("download", conversation=self.conversation, source=file_id, path=str(target))
        async with asyncio.timeout(5):
            while not any(path.stat().st_size >= 65536 for path in self.root.glob("truncated-source.bin.miniim-*.part")):
                await asyncio.sleep(0.01)
        source.write_bytes(payload[:65536])
        self.pause_download_at = 0
        for protocol in self.protocols:
            protocol.m_download_sender.notify()
        failed = await self.bob.wait("file-tasks",
            lambda item: any(task["status"] == "failed" for task in item["items"]), since=mark)
        task = next(task for task in failed["items"] if task["status"] == "failed")
        self.assertIn("interrupted", task["error"])
        self.assertEqual(b"existing", target.read_bytes())
        await self.alice.command("message", conversation=self.conversation, intent="after-file-reset", text="still connected")
        await self.bob.wait("message", lambda item: item["clientMsgId"] == "after-file-reset", since=mark)
        source.write_bytes(payload)
        mark = await self.bob.command("retry-file", intent=task["clientFileId"])
        await self.bob.wait("file-tasks", lambda item: not item["items"], since=mark)
        self.assertEqual(payload, target.read_bytes())
        _, attempts = self.assert_single_file_intent("bob", 2)
        self.assertGreaterEqual(attempts[-1]["offset"], 65536)

    async def test_corrupt_download_can_retry_original_intent_from_zero(self):
        payload = b"verified retry" * 4096
        file_id = await self.upload(payload)
        source = self.files.get_storage_path(file_id)
        source.write_bytes(b"x" * len(payload))
        target = self.root / "retry-corrupt.bin"
        target.write_bytes(b"existing")
        mark = await self.bob.command("download", conversation=self.conversation, source=file_id, path=str(target))
        failed = await self.bob.wait("file-tasks",
            lambda item: any(task["status"] == "failed" for task in item["items"]), since=mark)
        task = next(task for task in failed["items"] if task["status"] == "failed")
        source.write_bytes(payload)
        mark = await self.bob.command("retry-file", intent=task["clientFileId"])
        await self.bob.wait("file-tasks", lambda item: not item["items"], since=mark)
        self.assertEqual(payload, target.read_bytes())
        _, attempts = self.assert_single_file_intent("bob", 2)
        self.assertEqual(0, attempts[-1]["offset"])

    async def test_file_tasks_wait_beyond_eight_active_downloads(self):
        payload = b"queued file" * 8192
        file_id = await self.upload(payload)
        self.pause_download_at = 65536
        targets = [self.root / f"queue-{index}.bin" for index in range(9)]
        mark = len(self.bob.events)
        for target in targets:
            await self.bob.command("download", conversation=self.conversation, source=file_id, path=str(target))
        async with asyncio.timeout(5):
            while len([item for item in self.file_attempts if item["user"] == "bob"]) < 8:
                await asyncio.sleep(0.01)
        await self.bob.command("message", conversation=self.conversation, intent="queue-barrier", text="responsive")
        await self.alice.wait("message", lambda item: item["clientMsgId"] == "queue-barrier")
        self.assertEqual(8, len([item for item in self.file_attempts if item["user"] == "bob"]))
        self.pause_download_at = 0
        for protocol in self.protocols:
            protocol.m_download_sender.notify()
        await self.bob.wait("file-tasks", lambda item: not item["items"], since=mark)
        for target in targets:
            self.assertEqual(payload, target.read_bytes())
        self.assertEqual(9, self.db.execute_fetchone(
            "SELECT COUNT(*) FROM file_transfers WHERE direction=2 AND status='completed'")[0])

    async def test_queued_file_cancel_bypasses_eight_stalled_downloads(self):
        payload = b"queued cancellation" * 8192
        file_id = await self.upload(payload)
        self.pause_download_at = 65536
        targets = [self.root / f"cancel-queue-{index}.bin" for index in range(9)]
        targets[-1].write_bytes(b"keep queued target")
        mark = len(self.bob.events)
        for target in targets:
            await self.bob.command("download", conversation=self.conversation, source=file_id, path=str(target))
        tasks = await self.bob.wait("file-tasks", lambda item: len(item["items"]) == 9, since=mark)
        intent = tasks["items"][-1]["clientFileId"]
        async with asyncio.timeout(5):
            while not any(protocol.m_user_id == "bob" and sum(job.offset >= 65536
                    for job in protocol.m_download_sender.jobs.values()) == 8 for protocol in self.protocols):
                await asyncio.sleep(0.01)
        mark = await self.bob.command("cancel-file", intent=intent)
        await self.bob.wait("file-tasks", lambda item: len(item["items"]) == 8
                           and all(task["clientFileId"] != intent for task in item["items"]), since=mark, timeout=8)
        self.assertEqual(1, self.db.execute_fetchone(
            "SELECT COUNT(*) FROM file_cancellations WHERE owner_id='bob' AND client_file_id=?", (intent,))[0])
        self.assertEqual(0, self.db.execute_fetchone(
            "SELECT COUNT(*) FROM file_transfers WHERE owner_id='bob' AND client_file_id=?", (intent,))[0])
        self.assertEqual(b"keep queued target", targets[-1].read_bytes())
        self.pause_download_at = 0
        for protocol in self.protocols:
            protocol.m_download_sender.notify()
        await self.bob.wait("file-tasks", lambda item: not item["items"], since=mark)
        for target in targets[:-1]:
            self.assertEqual(payload, target.read_bytes())
        self.assertEqual(8, self.db.execute_fetchone(
            "SELECT COUNT(*) FROM file_transfers WHERE direction=2 AND status='completed'")[0])

    async def test_pending_upload_isolated_while_switching_accounts(self):
        self.fault.file_drop_after_bytes = 262144
        payload = b"account-private" * 65536
        source = self.root / "private-upload.bin"
        source.write_bytes(payload)
        mark = await self.alice.command("upload", conversation=self.conversation, path=str(source))
        await self.alice.wait("connection", lambda item: item["state"] == "reconnecting", since=mark)
        await self.disconnect(self.alice)
        self.fault.file_drop_after_bytes = 0
        initial = await self.alice.connect(self.endpoint, "carol")
        self.assertEqual([], initial["fileTasks"])
        await self.synced(self.alice, "carol")
        self.assertEqual(["alice"], [item["user"] for item in self.file_attempts])
        await self.disconnect(self.alice)
        mark = len(self.alice.events)
        await self.alice.connect(self.endpoint, "alice")
        await self.alice.wait("file-tasks", lambda item: not item["items"], since=mark)
        self.assert_single_file_intent("alice", 1)

    async def test_second_native_device_recovers_upload_after_owner_crash_and_lease_expiry(self):
        self.hold_alice_upload = True
        self.hold_upload_finish = True
        payload = b"same intent across devices" * 16384
        source = self.root / "two-device-upload.bin"
        source.write_bytes(payload)
        await self.alice.command("upload", conversation=self.conversation, path=str(source))
        async with asyncio.timeout(5):
            while not self.held_upload_chunks:
                await asyncio.sleep(0.01)
        state_root = self.root / "state-alice-second"
        second = await NativeClient.start(self.driver_path,
            self.output_dir / f"{self._testMethodName}-second-device.log", state_root)
        self.clients.append(second)
        await second.connect(self.endpoint, "alice", device="device-alice-second")
        await self.synced(second, "alice")
        await self.disconnect(second)
        with closing(sqlite3.connect(next((self.root / "state-alice").glob("*.sqlite")))) as original:
            row = original.execute(
                "SELECT id,init_request,finish_request,file_id,cancel_request,status,data FROM file_tasks").fetchone()
        with closing(sqlite3.connect(next(state_root.glob("*.sqlite")))) as destination:
            destination.execute("INSERT INTO file_tasks(id,init_request,finish_request,file_id,cancel_request,status,data) "
                "VALUES(?,?,?,?,?,?,?)", row)
            destination.commit()
        mark = len(second.events)
        initial = await second.connect(self.endpoint, "alice", device="device-alice-second")
        self.assertEqual(row[0], initial["fileTasks"][0]["clientFileId"])
        async with asyncio.timeout(5):
            while not any(item["device"] == "device-alice-second" for item in self.upload_conflicts):
                await asyncio.sleep(0.01)
        before = self.files.get_transfer_by_file_id(row[3]).received_bytes
        protocol, stream_id, data, end_stream = self.held_upload_chunks.pop(0)
        super(TestProtocol, protocol)._handle_file_stream_data(stream_id, data, end_stream)
        await second.wait("file", lambda item: item["fileId"] == row[3] and item["transferredBytes"] > before, since=mark)
        await self.alice.crash()
        # Keep the production timeout unchanged; shorten only this isolated takeover wait.
        self.hub.uploads.idle_timeout_ms = 1000
        self.hold_alice_upload = False
        self.hold_upload_finish = False
        await second.wait("file-tasks", lambda item: not item["items"], since=mark, timeout=14)
        transfer, attempts = self.assert_single_file_intent("alice", 1)
        self.assertEqual(row[3], transfer["file_id"])
        self.assertGreater(attempts[-1]["acceptedOffset"], before)
        self.assertEqual(payload, self.files.get_storage_path(row[3]).read_bytes())
        self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])

    async def test_upload_connection_drop_does_not_complete(self):
        self.fault.file_drop_after_bytes = 65536
        source = self.root / "interrupted.bin"
        source.write_bytes(bytes(range(256)) * 8192)
        mark = await self.alice.command("upload", conversation=self.conversation, path=str(source))
        await self.alice.wait("connection", lambda item: item["state"] == "disconnected", since=mark)
        transfers = self.db.execute_fetchall("SELECT status, received_bytes, file_size FROM file_transfers")
        self.assertEqual(1, len(transfers))
        self.assertNotEqual("completed", transfers[0]["status"])
        self.assertLess(transfers[0]["received_bytes"], self.fault.file_drop_after_bytes)
        self.assertFalse(any(item["event"] == "file" and item["data"]["completed"] for item in self.alice.events[mark:]))
        self.assertEqual(0, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", type=Path, default=ROOT / "build/client-manifest/Release/mini_im_native_driver.exe")
    parser.add_argument("--output", type=Path, default=ROOT / "tmp/native-integration")
    parser.add_argument("--test", action="append", help="run one named test method; may be repeated")
    args = parser.parse_args()
    if not args.client.is_file():
        parser.error("build the mini_im_native_driver target before running this test")
    output = args.output.resolve() / time.strftime("%Y%m%d-%H%M%S")
    output.mkdir(parents=True, exist_ok=True)
    NativeFlowTest.driver_path = args.client.resolve()
    NativeFlowTest.output_dir = output
    if args.test:
        for name in args.test:
            if not name.startswith("test_") or not callable(getattr(NativeFlowTest, name, None)):
                parser.error(f"unknown test method: {name}")
        suite = unittest.TestSuite(NativeFlowTest(name) for name in args.test)
    else:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(NativeFlowTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    (output / "results.json").write_text(json.dumps({
        "tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "successful": result.wasSuccessful(), "client": str(args.client.resolve()),
        "scope": "Qt desktop bridge + native MsQuic core; loopback QUIC; no Vue browser",
    }, indent=2), encoding="utf-8")
    print(f"Native integration evidence: {output}")
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
