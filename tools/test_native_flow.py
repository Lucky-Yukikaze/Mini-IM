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
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from quic.endpoint import serve_quic
from aioquic.quic.configuration import QuicConfiguration

from protocol.pb import common_pb2, message_pb2
from quic.server import FaultConfig, MiniImQuicProtocol, OnlineSessionHub, ensure_dev_cert
from services.auth.service import AuthService
from services.conversation.service import ConversationService
from services.delivery.service import DeliveryService
from services.file.service import FileService
from services.message.service import MessageService, SendMessageResult
from services.sync.service import SyncService
from storage.repo import ConversationRepo, DeliveryRepo, FileRepo, MessageRepo, SyncRepo
from storage.sqlite.db import MiniImSqliteDb
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

    async def connect(self, endpoint, user):
        mark = await self.command("connect", endpoint=endpoint, token="dev-token:" + user, device="device-" + user)
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

    def _debug(self, message):
        self.scenario.server_log.write(message + "\n")
        self.scenario.server_log.flush()

    def send_sync_event(self, event):
        if self.m_user_id == "bob" and event.event_type == "message" and self.scenario.drop_next_bob_message:
            self.scenario.drop_next_bob_message = False
            return
        super().send_sync_event(event)

    def _send(self, stream_id, envelope):
        if envelope.HasField("ack") and envelope.ack.success:
            key = "filefinish" if "-filefinish-" in envelope.request_id else "fileinit"
            if ("-filefinish-" in envelope.request_id or "-fileinit-" in envelope.request_id
                    or "-filedl-" in envelope.request_id) and self.scenario.drop_file_acks.get(key, 0):
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


class RecordingFileService(FileService):
    def __init__(self, *args, scenario, **kwargs):
        super().__init__(*args, **kwargs)
        self.scenario = scenario

    def handle_file_init(self, user_id, request_id, file_init):
        result = super().handle_file_init(user_id, request_id, file_init)
        self.scenario.file_attempts.append({
            "user": user_id, "requestId": request_id, "intent": file_init.client_file_id,
            "offset": file_init.resume_offset, "fileId": result.ack.entity_id,
        })
        return result


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
                delivery_service=DeliveryService(deliveries),
                file_service=self.files, message_service=RecordingMessageService(messages, conversations, scenario=self),
                sync_service=SyncService(SyncRepo(self.db)), online_hub=self.hub, fault_config=self.fault, **kwargs,
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
        (self.output_dir / f"{self._testMethodName}-sync-replies.json").write_text(
            json.dumps(self.sync_replies, indent=2), encoding="utf-8")
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
        for content, error in ((payload[:-1], "incomplete"), (b"x" * len(payload), "sha256 mismatch")):
            with self.subTest(error=error):
                stored.write_bytes(content)
                target = self.root / "protected.bin"
                target.write_bytes(b"previous content")
                mark = await self.bob.command("download", conversation=self.conversation, source=file_id, path=str(target))
                await self.bob.wait("error", lambda item: error in item["message"], since=mark)
                self.assertEqual(b"previous content", target.read_bytes())
                rows = self.db.execute_fetchall("SELECT status FROM file_transfers WHERE direction = ?", (common_pb2.FILE_DIRECTION_DOWNLOAD,))
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
    parser.add_argument("--client", type=Path, default=ROOT / "build/client_qt611/Release/mini_im_native_driver.exe")
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
