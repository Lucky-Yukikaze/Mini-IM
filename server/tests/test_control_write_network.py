"""Real QUIC checks for durable writes, replay and storage error replies."""
import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
import ssl
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aioquic.asyncio import connect, QuicConnectionProtocol
from aioquic.quic.events import StopSendingReceived
from aioquic.quic.configuration import QuicConfiguration
from protocol.codec import EnvelopeCodec
from protocol.pb import auth_pb2, common_pb2, conversation_pb2, envelope_pb2, file_pb2, message_pb2, sync_pb2
from quic.endpoint import serve_quic
from quic.server import FaultConfig, MiniImQuicProtocol, OnlineSessionHub, ensure_dev_cert
from services.auth.service import AuthService
from services.conversation.service import ConversationService
from services.control.service import ControlWriteService
from services.delivery.service import DeliveryService
from services.file.service import FileService
from services.message.service import MessageService
from services.sync.service import SyncService
from storage.repo import ConversationRepo, DeliveryRepo, FileRepo, MessageRepo, SyncRepo
from storage.sqlite.db import MiniImSqliteDb
from storage.repo.control_write_repo import ControlWriteRepo
from storage.sqlite.init_db import init_db


class UploadSender:
    def __init__(self, protocol):
        self.protocol = protocol
        self.stream_id = protocol._quic.get_next_available_stream_id(is_unidirectional=True)

    def write(self, data):
        self.protocol._quic.send_stream_data(self.stream_id, data)
        self.protocol.transmit()

    def write_eof(self):
        self.protocol._quic.send_stream_data(self.stream_id, b"", end_stream=True)
        self.protocol.transmit()

    def get_extra_info(self, name):
        assert name == "stream_id"
        return self.stream_id


class ObservedPeer(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stopped = {}

    def quic_event_received(self, event):
        if isinstance(event, StopSendingReceived):
            self.stopped[event.stream_id] = event.error_code
        super().quic_event_received(event)


class ControlWriteNetworkTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = None
        self.server = None
        self.drop_ack = set()
        self.addAsyncCleanup(self.cleanup)
        cert, key = self.root / "cert.pem", self.root / "key.pem"
        ensure_dev_cert(cert, key)
        self.config = QuicConfiguration(is_client=False, alpn_protocols=["mini-im"])
        self.config.load_cert_chain(str(cert), str(key))
        await self.start_server()
        self.conversations.ensure_user("alice")
        self.conversations.ensure_user("bob")
        created = self.conversations.handle_create_conversation(
            "alice", "setup", conversation_pb2.CreateConversation(
                client_conv_id="group", type=common_pb2.CONVERSATION_GROUP,
                member_ids=["bob"], title="original"))
        self.conversation = created.ack.entity_id

    async def start_server(self):
        init_db(self.root / "test.db")
        self.db = MiniImSqliteDb(self.root / "test.db")
        conversations, messages = ConversationRepo(self.db), MessageRepo(self.db)
        self.conversations = ConversationService(conversations)
        deliveries = DeliveryService(DeliveryRepo(self.db))
        files = self.files = FileService(FileRepo(self.db), conversations, messages, self.root / "files", 900000)
        auth, hub = AuthService(), OnlineSessionHub()
        self.uploads = hub.uploads
        dropped = self.drop_ack
        rejected = self.rejected_uploads = []

        class FaultProtocol(MiniImQuicProtocol):
            def _reject_file_stream(self, stream_id):
                super()._reject_file_stream(stream_id)
                rejected.append((self.m_device_id, stream_id))

            def _send(self, stream_id, envelope):
                if envelope.HasField("ack") and envelope.request_id in dropped:
                    dropped.remove(envelope.request_id)
                    return
                super()._send(stream_id, envelope)

            def _debug(self, message):
                pass

        self.server = await serve_quic(
            "127.0.0.1", 0, configuration=self.config,
            create_protocol=lambda *args, **kwargs: FaultProtocol(
                *args, auth_service=auth, conversation_service=self.conversations,
                control_write_service=ControlWriteService(ControlWriteRepo(self.db), self.conversations, deliveries),
                file_service=files,
                message_service=MessageService(messages, conversations),
                sync_service=SyncService(SyncRepo(self.db)), online_hub=hub,
                fault_config=FaultConfig(), **kwargs))
        self.port = self.server._transport.get_extra_info("sockname")[1]

    async def cleanup(self):
        if self.server:
            self.server.close()
        if self.db:
            self.db.close()
        self.temp.cleanup()

    @staticmethod
    async def read(reader, request_id=None, body=None):
        async def receive():
            while True:
                size = struct.unpack(">I", await reader.readexactly(4))[0]
                envelope = EnvelopeCodec.decode(await reader.readexactly(size))
                if ((request_id is None or envelope.request_id == request_id)
                        and (body is None or envelope.HasField(body))):
                    return envelope
        return await asyncio.wait_for(receive(), 3)

    @asynccontextmanager
    async def peer(self, user="alice", device="first-device", with_protocol=False, claimed_device=None):
        config = QuicConfiguration(is_client=True, alpn_protocols=["mini-im"])
        config.verify_mode = ssl.CERT_NONE
        async with connect("127.0.0.1", self.port, configuration=config, create_protocol=ObservedPeer) as protocol:
            reader, writer = await protocol.create_stream()
            hello = envelope_pb2.Envelope(version=1, request_id="hello", channel=common_pb2.CHANNEL_CONTROL)
            hello.hello.token = "dev-token:" + user
            hello.hello.device_id = device
            writer.write(EnvelopeCodec.encode_frame(hello))
            welcome = await self.read(reader, "hello", "welcome")

            def send(request_id, **body):
                envelope = envelope_pb2.Envelope(
                    version=1, request_id=request_id, channel=common_pb2.CHANNEL_CONTROL,
                    session_id=welcome.welcome.session_id, device_id=claimed_device if claimed_device is not None else device, **body)
                writer.write(EnvelopeCodec.encode_frame(envelope))
            yield (reader, send, protocol) if with_protocol else (reader, send)
            writer.close()


    async def test_message_intent_conflict_from_second_device_survives_server_reopen(self):
        request = message_pb2.SendMessage(conversation_id=self.conversation, client_msg_id="shared-intent",
            type=common_pb2.MSG_TEXT, content=b"original", burn_mode=1, burn_ttl_sec=5)
        async with self.peer() as (reader, send):
            send("original-request", send_message=request)
            original = (await self.read(reader, "original-request", "ack")).ack
            self.assertTrue(original.success)
        before = self.event_count()
        for reopen in (False, True):
            if reopen:
                self.server.close()
                await asyncio.sleep(0.05)
                self.db.close()
                await self.start_server()
            async with self.peer(device="second-device") as (reader, send):
                changed = message_pb2.SendMessage(); changed.CopyFrom(request); changed.content = b"conflicting"
                send("conflicting-request", send_message=changed)
                conflict = (await self.read(reader, "conflicting-request", "ack")).ack
                self.assertFalse(conflict.success)
                self.assertEqual(409, conflict.code)
                send("original-retry", send_message=request)
                replay = (await self.read(reader, "original-retry", "ack")).ack
                self.assertTrue(replay.success)
                self.assertEqual(original.entity_id, replay.entity_id)
                self.assertEqual(before, self.event_count())
                self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])
                self.assertEqual(b"original", self.db.execute_fetchone("SELECT content FROM messages")[0])

    async def test_message_request_id_cross_operation_conflicts_and_ack_replay(self):
        request = message_pb2.SendMessage(conversation_id=self.conversation, client_msg_id="request-intent",
            type=common_pb2.MSG_TEXT, content=b"request body")
        async with self.peer() as (reader, send):
            send("shared-request", send_message=request)
            original = (await self.read(reader, "shared-request", "ack")).ack
            self.assertTrue(original.success)
        self.server.close()
        await asyncio.sleep(0.05)
        self.db.close()
        await self.start_server()
        async with self.peer(device="request-second-device") as (reader, send):
            before = self.event_count()
            changed = message_pb2.SendMessage(); changed.CopyFrom(request); changed.client_msg_id = "another-intent"
            send("shared-request", send_message=changed)
            self.assertEqual(409, (await self.read(reader, "shared-request", "ack")).ack.code)
            rename = conversation_pb2.RenameConversation(conversation_id=self.conversation, title="changed")
            send("shared-request", rename_conversation=rename)
            self.assertEqual(409, (await self.read(reader, "shared-request", "ack")).ack.code)
            send("shared-request", send_message=request)
            self.assertEqual(original.SerializeToString(), (await self.read(reader, "shared-request", "ack")).ack.SerializeToString())
            self.assertEqual(before, self.event_count())
            send("control-owned", rename_conversation=rename)
            self.assertTrue((await self.read(reader, "control-owned", "ack")).ack.success)
            send("control-owned", send_message=request)
            self.assertEqual(409, (await self.read(reader, "control-owned", "ack")).ack.code)
            self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])

    async def test_message_result_commit_failure_returns_retryable_error(self):
        self.db.execute_write("CREATE TRIGGER fail_message_result BEFORE INSERT ON control_write_results "
                              "WHEN NEW.operation='send_message' BEGIN SELECT RAISE(ABORT,'message result failure'); END")
        async with self.peer() as (reader, send):
            request = message_pb2.SendMessage(conversation_id=self.conversation, client_msg_id="retry-commit",
                type=common_pb2.MSG_TEXT, content=b"atomic result")
            before = self.event_count()
            send("retry-commit", send_message=request)
            self.assertEqual(503, (await self.read(reader, "retry-commit", "error")).error.code)
            self.assertEqual(0, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])
            self.assertEqual(0, self.db.execute_fetchone("SELECT COUNT(*) FROM control_write_results")[0])
            self.assertEqual(before, self.event_count())
            self.db.execute_write("DROP TRIGGER fail_message_result")
            send("retry-commit", send_message=request)
            ack = (await self.read(reader, "retry-commit", "ack")).ack
            self.assertTrue(ack.success)
            self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])
            self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM control_write_results")[0])

    async def send_delivery_message(self):
        async with self.peer() as (reader, send):
            send("offline-message", send_message=message_pb2.SendMessage(
                conversation_id=self.conversation, client_msg_id="offline-message",
                type=common_pb2.MSG_TEXT, content=b"offline body"))
            ack = (await self.read(reader, "offline-message", "ack")).ack
            self.assertTrue(ack.success)
        cursor = self.db.execute_fetchone(
            "SELECT seq FROM sync_events WHERE user_id='bob' AND entity_id=?", (ack.entity_id,))[0]
        return ack.entity_id, cursor

    def delivery_row(self, message):
        return dict(self.db.execute_fetchone(
            "SELECT * FROM message_deliveries WHERE server_msg_id=? AND user_id='bob'", (message,)))

    async def test_delivery_confirmation_uses_authenticated_device_and_preserves_first_delivery(self):
        message, cursor = await self.send_delivery_message()
        before = self.delivery_row(message)
        self.assertEqual("sent", before["status"])
        self.assertIsNone(before["delivered_at_ms"])
        request = sync_pb2.SyncApplied(global_cursor=cursor)
        async with self.peer(user="bob", device="actual-device", claimed_device="forged-device") as (reader, send):
            send("fetch", sync_request=sync_pb2.SyncRequest(global_cursor=0, limit=200))
            await self.read(reader, "fetch", "sync_response")
            self.assertEqual(before, self.delivery_row(message))
            send("received", sync_applied=request)
            original = (await self.read(reader, "received", "ack")).ack
            self.assertTrue(original.success)
            observer = MiniImSqliteDb(self.root / "test.db")
            try:
                self.assertEqual("delivered", observer.execute_fetchone(
                    "SELECT status FROM message_deliveries WHERE server_msg_id=? AND user_id='bob'", (message,))[0])
                self.assertEqual("actual-device", observer.execute_fetchone(
                    "SELECT device_id FROM sync_applied_cursors WHERE user_id='bob'")[0])
                self.assertIsNotNone(observer.execute_fetchone(
                    "SELECT ack FROM control_write_results WHERE request_id='received'"))
            finally:
                observer.close()
            pushed = (await self.read(reader, body="sync_response")).sync_response.events[0]
            self.assertEqual(message, pushed.delivery_updated.message_id)
            self.assertEqual("bob", pushed.delivery_updated.user_id)
            delivered = self.delivery_row(message)
            send("received", sync_applied=request)
            self.assertEqual(original, (await self.read(reader, "received", "ack")).ack)
        async with self.peer(user="bob", device="second-device") as (reader, send):
            send("second", sync_applied=request)
            self.assertTrue((await self.read(reader, "second", "ack")).ack.success)
        self.assertEqual(delivered, self.delivery_row(message))
        self.assertEqual(2, self.db.execute_fetchone(
            "SELECT COUNT(*) FROM sync_events WHERE event_type='delivery_updated'")[0])

    async def test_delivery_confirmation_lost_ack_survives_server_reopen(self):
        message, cursor = await self.send_delivery_message()
        request = sync_pb2.SyncApplied(global_cursor=cursor)
        self.drop_ack.add("received")
        async with self.peer(user="bob") as (reader, send):
            send("received", sync_applied=request)
            await self.read(reader, body="sync_response")
        self.assertNotIn("received", self.drop_ack)
        delivered = self.delivery_row(message)
        ack = bytes(self.db.execute_fetchone(
            "SELECT ack FROM control_write_results WHERE request_id='received'")[0])
        count = self.event_count()
        self.server.close()
        self.db.close()
        await self.start_server()
        async with self.peer(user="bob") as (reader, send):
            send("received", sync_applied=request)
            self.assertEqual(ack, (await self.read(reader, "received", "ack")).ack.SerializeToString())
        self.assertEqual(count, self.event_count())
        self.assertEqual(delivered, self.delivery_row(message))

    async def test_delivery_confirmation_failure_rolls_back_and_retries_over_quic(self):
        message, cursor = await self.send_delivery_message()
        request = sync_pb2.SyncApplied(global_cursor=cursor)
        before = self.delivery_row(message)
        count = self.event_count()
        self.db.execute_write("CREATE TRIGGER reject_confirmation BEFORE INSERT ON control_write_results "
                              "BEGIN SELECT RAISE(ABORT,'injected confirmation failure'); END")
        async with self.peer(user="bob") as (reader, send):
            send("received", sync_applied=request)
            self.assertEqual(503, (await self.read(reader, "received", "error")).error.code)
            self.assertEqual(before, self.delivery_row(message))
            self.assertEqual(count, self.event_count())
            self.assertEqual(0, self.db.execute_fetchone("SELECT COUNT(*) FROM sync_applied_cursors")[0])
            self.db.execute_write("DROP TRIGGER reject_confirmation")
            send("received", sync_applied=request)
            self.assertTrue((await self.read(reader, "received", "ack")).ack.success)
        self.assertEqual("delivered", self.delivery_row(message)["status"])

    def title(self):
        return self.db.execute_fetchone(
            "SELECT title FROM conversations WHERE conversation_id=?", (self.conversation,))[0]

    def event_count(self):
        return self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0]

    async def test_lost_ack_replay_after_server_reopen_preserves_later_rename(self):
        old = conversation_pb2.RenameConversation(conversation_id=self.conversation, title="old")
        new = conversation_pb2.RenameConversation(conversation_id=self.conversation, title="new")
        self.drop_ack.add("rename-old")
        async with self.peer() as (reader, send):
            send("rename-old", rename_conversation=old)
            await self.read(reader, body="sync_response")
            self.assertEqual("old", self.title())
            self.assertNotIn("rename-old", self.drop_ack)
            send("rename-new", rename_conversation=new)
            self.assertTrue((await self.read(reader, "rename-new", "ack")).ack.success)
        count = self.event_count()
        self.server.close()
        self.db.close()
        await self.start_server()
        async with self.peer(device="second-device") as (reader, send):
            send("rename-old", rename_conversation=old)
            self.assertTrue((await self.read(reader, "rename-old", "ack")).ack.success)
        self.assertEqual("new", self.title())
        self.assertEqual(count, self.event_count())

    async def test_reused_request_with_different_body_is_rejected(self):
        async with self.peer() as (reader, send):
            send("same-request", rename_conversation=conversation_pb2.RenameConversation(
                conversation_id=self.conversation, title="first"))
            original = (await self.read(reader, "same-request", "ack")).ack
            self.assertTrue(original.success)
            count = self.event_count()
            send("same-request", rename_conversation=conversation_pb2.RenameConversation(
                conversation_id=self.conversation, title="second"))
            conflict = (await self.read(reader, "same-request", "ack")).ack
            self.assertFalse(conflict.success)
            self.assertEqual(409, conflict.code)
            self.assertEqual("first", self.title())
            self.assertEqual(count, self.event_count())


    async def test_result_insert_failure_rolls_back_before_reply_and_allows_retry(self):
        count = self.event_count()
        self.db.execute_write(
            "CREATE TRIGGER reject_control_result BEFORE INSERT ON control_write_results "
            "BEGIN SELECT RAISE(ABORT, 'injected control result failure'); END")
        request = conversation_pb2.RenameConversation(conversation_id=self.conversation, title="committed")
        async with self.peer() as (reader, send):
            send("retry-after-storage-failure", rename_conversation=request)
            response = await self.read(reader, "retry-after-storage-failure")
            self.assertTrue(response.HasField("error"))
            self.assertEqual(503, response.error.code)
            self.assertEqual("original", self.title())
            self.assertEqual(count, self.event_count())
            self.assertEqual(0, self.db.execute_fetchone("SELECT COUNT(*) FROM control_write_results")[0])
            self.db.execute_write("DROP TRIGGER reject_control_result")
            send("retry-after-storage-failure", rename_conversation=request)
            response = await self.read(reader, "retry-after-storage-failure", "ack")
            self.assertTrue(response.ack.success)
            # A separate database connection sees both the result and business change before ACK is observed.
            observer = MiniImSqliteDb(self.root / "test.db")
            try:
                self.assertEqual("committed", observer.execute_fetchone(
                    "SELECT title FROM conversations WHERE conversation_id=?", (self.conversation,))[0])
                self.assertEqual(count + 2, observer.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0])
                self.assertEqual(1, observer.execute_fetchone("SELECT COUNT(*) FROM control_write_results")[0])
            finally:
                observer.close()


    async def test_membership_reads_preserve_original_recipients_across_reopen(self):
        for user in ("carol", "dave"):
            self.conversations.ensure_user(user)

        async def accepted(reader, send, request_id, **body):
            send(request_id, **body)
            ack = (await self.read(reader, request_id, "ack")).ack
            self.assertTrue(ack.success, ack.message)
            return ack.entity_id

        def receipt(seq):
            return message_pb2.Receipt(conversation_id=self.conversation, last_read_seq=seq)

        def message(intent):
            return message_pb2.SendMessage(conversation_id=self.conversation, client_msg_id=intent,
                type=common_pb2.MSG_TEXT, content=intent.encode())

        def counts(message_id):
            return tuple(self.db.execute_fetchone("SELECT member_count,read_count,unread_count "
                "FROM message_read_counters WHERE server_msg_id=?", (message_id,)))

        async with self.peer() as (alice, send_alice), self.peer("bob") as (bob, send_bob), self.peer(
                "dave") as (dave, send_dave):
            await accepted(alice, send_alice, "add-carol", add_members=conversation_pb2.AddMembers(
                conversation_id=self.conversation, member_ids=["carol"]))
            first = await accepted(alice, send_alice, "before-join", send_message=message("before-join"))
            await accepted(dave, send_dave, "join-dave", join_conversation=conversation_pb2.JoinConversation(
                conversation_id=self.conversation))
            await accepted(dave, send_dave, "dave-read", receipt=receipt(1))
            self.assertEqual((2, 0, 2), counts(first))
            await accepted(bob, send_bob, "bob-read", receipt=receipt(1))
            await accepted(bob, send_bob, "bob-leave", leave_conversation=conversation_pb2.LeaveConversation(
                conversation_id=self.conversation))
            absent = await accepted(alice, send_alice, "while-absent", send_message=message("while-absent"))
            self.assertEqual((2, 1, 1), counts(first))
        self.server.close()
        self.db.close()
        await self.start_server()
        async with self.peer() as (alice, send_alice), self.peer("bob", device="second-device") as (bob, send_bob):
            send_bob("absent-read", receipt=receipt(2))
            rejected = (await self.read(bob, "absent-read", "ack")).ack
            self.assertFalse(rejected.success)
            self.assertEqual(403, rejected.code)
            await accepted(bob, send_bob, "bob-rejoin", join_conversation=conversation_pb2.JoinConversation(
                conversation_id=self.conversation))
            self.assertEqual(1, self.db.execute_fetchone("SELECT last_read_seq FROM conversation_members "
                "WHERE conversation_id=? AND user_id='bob'", (self.conversation,))[0])
            count = self.event_count()
            await accepted(bob, send_bob, "bob-repeat-read", receipt=receipt(1))
            self.assertEqual(count, self.event_count())
            latest = await accepted(alice, send_alice, "after-return", send_message=message("after-return"))
            await accepted(bob, send_bob, "bob-read-through", receipt=receipt(3))
            self.assertEqual((2, 1, 1), counts(first))
            self.assertEqual((2, 0, 2), counts(absent))
            self.assertEqual((3, 1, 2), counts(latest))
            count = self.event_count()
            await accepted(bob, send_bob, "bob-read-through", receipt=receipt(3))
            self.assertEqual(count, self.event_count())
            self.assertEqual([], self.db.execute_fetchall("PRAGMA foreign_key_check"))
            self.assertEqual("ok", self.db.execute_fetchone("PRAGMA integrity_check")[0])

    def upload_request(self):
        payload = b"file storage recovery"
        return payload, file_pb2.FileInit(conversation_id=self.conversation, client_file_id="upload",
            file_name="source.bin", file_size=len(payload), sha256=hashlib.sha256(payload).hexdigest(),
            direction=common_pb2.FILE_DIRECTION_UPLOAD)

    async def test_file_init_database_failure_returns_retryable_error_without_partial_rows(self):
        _, request = self.upload_request()
        count = self.event_count()
        self.db.execute_write("CREATE TRIGGER reject_file_init BEFORE INSERT ON file_transfers "
            "BEGIN SELECT RAISE(ABORT, 'injected initialization failure'); END")
        async with self.peer() as (reader, send):
            send("file-init", file_init=request)
            response = await self.read(reader, "file-init")
            self.assertEqual(503, response.error.code)
            self.assertEqual(count, self.event_count())
            self.assertEqual(0, self.db.execute_fetchone("SELECT COUNT(*) FROM file_transfers")[0])
            self.db.execute_write("DROP TRIGGER reject_file_init")
            send("file-init", file_init=request)
            self.assertTrue((await self.read(reader, "file-init", "ack")).ack.success)
            self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM file_transfers")[0])

    async def test_file_repair_disk_failure_returns_retryable_error(self):
        payload, request = self.upload_request()
        initialized = self.files.handle_file_init("alice", "setup-upload", request)
        file_id = initialized.ack.entity_id
        self.files.append_file_chunk("alice", file_id, payload[:5])
        path = self.files.get_storage_path(file_id)
        path.write_bytes(payload[:10])
        original = Path.open
        def unavailable(target, *args, **kwargs):
            if target == path:
                raise OSError("injected storage unavailable")
            return original(target, *args, **kwargs)
        async with self.peer() as (reader, send):
            with patch.object(Path, "open", unavailable):
                send("repair", file_init=request)
                response = await self.read(reader, "repair")
            self.assertEqual(503, response.error.code)
            self.assertEqual(payload[:10], path.read_bytes())
            self.assertEqual(5, self.files.get_transfer_by_file_id(file_id).received_bytes)
            send("repair", file_init=request)
            self.assertTrue((await self.read(reader, "repair", "ack")).ack.success)
            self.assertEqual(5, (await self.read(reader, "repair", "file_updated")).file_updated.transferred_bytes)
            self.assertEqual(payload[:5], path.read_bytes())

    async def test_file_finish_failure_rolls_back_completion_and_message_before_retry(self):
        payload, request = self.upload_request()
        initialized = self.files.handle_file_init("alice", "setup-upload", request)
        file_id = initialized.ack.entity_id
        self.files.append_file_chunk("alice", file_id, payload)
        count = self.event_count()
        self.db.execute_write("CREATE TRIGGER reject_file_message BEFORE INSERT ON messages "
            "BEGIN SELECT RAISE(ABORT, 'injected file message failure'); END")
        finish = file_pb2.FileFinish(file_id=file_id, success=True)
        async with self.peer() as (reader, send):
            send("finish", file_finish=finish)
            response = await self.read(reader, "finish")
            self.assertEqual(503, response.error.code)
            self.assertEqual("uploaded", self.files.get_transfer_by_file_id(file_id).status)
            self.assertEqual(0, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])
            self.assertEqual(count, self.event_count())
            self.db.execute_write("DROP TRIGGER reject_file_message")
            send("finish", file_finish=finish)
            self.assertTrue((await self.read(reader, "finish", "ack")).ack.success)
            self.assertEqual("completed", self.files.get_transfer_by_file_id(file_id).status)
            self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])


    def shared_upload_request(self, payload, intent="shared-upload"):
        return file_pb2.FileInit(conversation_id=self.conversation, client_file_id=intent,
            file_name="shared.bin", file_size=len(payload), sha256=hashlib.sha256(payload).hexdigest(),
            direction=common_pb2.FILE_DIRECTION_UPLOAD)

    async def test_same_upload_intent_waits_for_active_device(self):
        request = self.shared_upload_request(b"exclusive writer")
        async with self.peer(device="first") as (first, send_first), self.peer(device="second") as (second, send_second):
            send_first("first-init", file_init=request)
            accepted = await self.read(first, "first-init", "ack")
            self.assertTrue(accepted.ack.success)
            count = self.event_count()
            send_second("second-init", file_init=request)
            response = await self.read(second, "second-init")
            self.assertEqual(429, response.error.code)
            self.assertEqual(count, self.event_count())
            self.assertEqual(0, self.files.get_transfer_by_file_id(accepted.ack.entity_id).received_bytes)

    async def test_other_device_cannot_fail_active_upload(self):
        request = self.shared_upload_request(b"protected active task")
        async with self.peer(device="first") as (first, send_first), self.peer(device="second") as (second, send_second):
            send_first("first-init", file_init=request)
            file_id = (await self.read(first, "first-init", "ack")).ack.entity_id
            count = self.event_count()
            send_second("late-finish", file_finish=file_pb2.FileFinish(file_id=file_id, success=False))
            response = await self.read(second, "late-finish")
            self.assertEqual(429, response.error.code)
            self.assertEqual("init", self.files.get_transfer_by_file_id(file_id).status)
            self.assertEqual(count, self.event_count())


    @staticmethod
    async def until(predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.01)

    async def test_init_request_conflicts_after_reopen_and_resumes_latest_bytes(self):
        payload = b"persistent init identity"
        request = self.shared_upload_request(payload)
        async with self.peer(with_protocol=True) as (reader, send, protocol):
            updated = await self.initialize_upload(reader, send, request, "reserved-init")
            writer = await self.upload_stream(protocol, updated.file_id, 0, payload[:8])
            await self.until(lambda: self.files.get_transfer_by_file_id(updated.file_id).received_bytes == 8)
            send("reserved-init", file_finish=file_pb2.FileFinish(file_id=updated.file_id, success=True))
            self.assertEqual(409, (await self.read(reader, "reserved-init", "ack")).ack.code)
        self.server.close(); await asyncio.sleep(0.05); self.db.close(); await self.start_server()
        async with self.peer(device="second-device", with_protocol=True) as (reader, send, protocol):
            message = message_pb2.SendMessage(conversation_id=self.conversation, client_msg_id="conflict",
                type=common_pb2.MSG_TEXT, content=b"must not write")
            send("reserved-init", send_message=message)
            self.assertEqual(409, (await self.read(reader, "reserved-init", "ack")).ack.code)
            changed = file_pb2.FileInit(); changed.CopyFrom(request); changed.client_file_id = "changed-intent"
            send("reserved-init", file_init=changed)
            self.assertEqual(409, (await self.read(reader, "reserved-init", "ack")).ack.code)
            request.resume_offset = 8; request.priority = 4
            resumed = await self.initialize_upload(reader, send, request, "reserved-init")
            self.assertEqual((updated.file_id, 8), (resumed.file_id, resumed.transferred_bytes))
            writer = await self.upload_stream(protocol, updated.file_id, 8, payload[8:]); writer.write_eof()
            await self.until(lambda: self.files.get_transfer_by_file_id(updated.file_id).received_bytes == len(payload))
            send("finish-original", file_finish=file_pb2.FileFinish(file_id=updated.file_id, success=True))
            self.assertTrue((await self.read(reader, "finish-original", "ack")).ack.success)
            self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])
            self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM file_transfers")[0])
            self.assertEqual(payload, self.files.get_storage_path(updated.file_id).read_bytes())

    async def test_init_identity_insert_error_rolls_back_and_retries_over_network(self):
        self.db.execute_write("CREATE TRIGGER fail_init_identity BEFORE INSERT ON file_init_requests "
                              "BEGIN SELECT RAISE(ABORT,'identity failure'); END")
        async with self.peer() as (reader, send):
            request = self.shared_upload_request(b"retry initialization")
            before = self.event_count()
            send("retry-init", file_init=request)
            self.assertEqual(503, (await self.read(reader, "retry-init", "error")).error.code)
            self.assertEqual(0, self.db.execute_fetchone("SELECT COUNT(*) FROM file_transfers")[0])
            self.assertEqual(0, self.db.execute_fetchone("SELECT COUNT(*) FROM file_init_requests")[0])
            self.assertEqual(before, self.event_count())
            self.db.execute_write("DROP TRIGGER fail_init_identity")
            await self.initialize_upload(reader, send, request, "retry-init")
            self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM file_init_requests")[0])

    async def initialize_upload(self, reader, send, request, request_id="init"):
        send(request_id, file_init=request)
        ack = (await self.read(reader, request_id, "ack")).ack
        self.assertTrue(ack.success, ack.message)
        return (await self.read(reader, request_id, "file_updated")).file_updated

    async def upload_stream(self, protocol, file_id, offset, chunk):
        writer = UploadSender(protocol)
        writer.write(f"MINIIMFILE2 {file_id} {offset}\n".encode() + chunk)
        return writer

    async def rejected_stream(self, protocol, writer):
        stream_id = writer.get_extra_info("stream_id")
        await self.until(lambda: stream_id in protocol.stopped)
        self.assertEqual(0x1006, protocol.stopped[stream_id])

    async def finish_upload(self, reader, send, file_id, payload):
        await self.until(lambda: self.files.get_transfer_by_file_id(file_id).received_bytes == len(payload))
        send("finish", file_finish=file_pb2.FileFinish(file_id=file_id, success=True))
        self.assertTrue((await self.read(reader, "finish", "ack")).ack.success)
        self.assertEqual(payload, self.files.get_storage_path(file_id).read_bytes())
        self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])

    async def test_file_completion_can_overtake_upload_bytes_without_failing_intent(self):
        payload = b"control stream can arrive before file stream"
        request = self.shared_upload_request(payload)
        async with self.peer(with_protocol=True) as (reader, send, protocol):
            updated = await self.initialize_upload(reader, send, request)
            writer = await self.upload_stream(protocol, updated.file_id, 0, payload[:8])
            await self.until(lambda: self.files.get_transfer_by_file_id(updated.file_id).received_bytes == 8)
            count = self.event_count()
            finish = file_pb2.FileFinish(file_id=updated.file_id, success=True)
            send("finish-before-bytes", file_finish=finish)
            waiting = (await self.read(reader, "finish-before-bytes", "ack")).ack
            self.assertFalse(waiting.success)
            self.assertEqual(503, waiting.code)
            self.assertEqual(count, self.event_count())
            self.assertEqual(0, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])
            writer.write(payload[8:])
            writer.write_eof()
            await self.until(lambda: self.files.get_transfer_by_file_id(updated.file_id).received_bytes == len(payload))
            send("finish-before-bytes", file_finish=finish)
            self.assertTrue((await self.read(reader, "finish-before-bytes", "ack")).ack.success)
            self.assertEqual(payload, self.files.get_storage_path(updated.file_id).read_bytes())
            count = self.event_count()
            send("finish-before-bytes", file_finish=finish)
            self.assertTrue((await self.read(reader, "finish-before-bytes", "ack")).ack.success)
            self.assertEqual(count, self.event_count())
            self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])

    async def test_duplicate_upload_stream_cannot_append_into_active_stream(self):
        payload = b"first halfsecond half"
        request = self.shared_upload_request(payload)
        async with self.peer(with_protocol=True) as (reader, send, protocol):
            updated = await self.initialize_upload(reader, send, request)
            writer = await self.upload_stream(protocol, updated.file_id, 0, payload[:10])
            await self.until(lambda: self.files.get_transfer_by_file_id(updated.file_id).received_bytes == 10)
            send("repeat-init", file_init=request)
            self.assertEqual(429, (await self.read(reader, "repeat-init")).error.code)
            duplicate = await self.upload_stream(protocol, updated.file_id, 10, b"wrong bytes")
            await self.rejected_stream(protocol, duplicate)
            self.assertEqual(payload[:10], self.files.get_storage_path(updated.file_id).read_bytes())
            writer.write(payload[10:])
            writer.write_eof()
            await self.finish_upload(reader, send, updated.file_id, payload)

    async def test_upload_requires_initialized_connection_and_exact_start_offset(self):
        payload = b"verified file content"
        request = self.shared_upload_request(payload)
        file_id = self.files.handle_file_init("alice", "seed", request).ack.entity_id
        async with self.peer(with_protocol=True) as (reader, send, protocol):
            send("authenticate", heartbeat=auth_pb2.Heartbeat())
            await self.read(reader, "authenticate", "heartbeat")
            unauthorized = await self.upload_stream(protocol, file_id, 0, b"incorrect data")
            await self.rejected_stream(protocol, unauthorized)
            self.assertEqual(0, self.files.get_transfer_by_file_id(file_id).received_bytes)
            await self.initialize_upload(reader, send, request)
            wrong_offset = await self.upload_stream(protocol, file_id, 4, b"wrong offset")
            await self.rejected_stream(protocol, wrong_offset)
            self.assertEqual(0, self.files.get_transfer_by_file_id(file_id).received_bytes)
            writer = await self.upload_stream(protocol, file_id, 0, payload)
            writer.write_eof()
            await self.finish_upload(reader, send, file_id, payload)

    async def test_expired_upload_takeover_rejects_old_stream_and_old_disconnect(self):
        payload = b"prefix--remaining-content"
        request = self.shared_upload_request(payload)
        async with self.peer(device="old", with_protocol=True) as (old, send_old, old_protocol), self.peer(
                device="new", with_protocol=True) as (new, send_new, new_protocol):
            first = await self.initialize_upload(old, send_old, request)
            old_writer = await self.upload_stream(old_protocol, first.file_id, 0, payload[:8])
            await self.until(lambda: self.files.get_transfer_by_file_id(first.file_id).received_bytes == 8)
            self.uploads.by_file[first.file_id].touched -= 901
            invalid = file_pb2.FileInit()
            invalid.CopyFrom(request)
            invalid.sha256 = "0" * 64
            send_new("invalid-takeover", file_init=invalid)
            self.assertEqual(409, (await self.read(new, "invalid-takeover", "ack")).ack.code)
            self.assertEqual(8, self.uploads.by_file[first.file_id].offset)
            resumed = await self.initialize_upload(new, send_new, request)
            self.assertEqual(first.file_id, resumed.file_id)
            self.assertEqual(8, resumed.transferred_bytes)
            old_writer.write(b"stale old bytes")
            await self.rejected_stream(old_protocol, old_writer)
            self.assertEqual(payload[:8], self.files.get_storage_path(first.file_id).read_bytes())
            old_protocol.close()
            await old_protocol.wait_closed()
            writer = await self.upload_stream(new_protocol, resumed.file_id, 8, payload[8:])
            writer.write_eof()
            await self.finish_upload(new, send_new, resumed.file_id, payload)

    async def test_disconnect_releases_upload_for_second_device(self):
        payload = b"recover original intent"
        request = self.shared_upload_request(payload)
        async with self.peer(device="old", with_protocol=True) as (old, send_old, protocol):
            updated = await self.initialize_upload(old, send_old, request)
            writer = await self.upload_stream(protocol, updated.file_id, 0, payload[:8])
            await self.until(lambda: self.files.get_transfer_by_file_id(updated.file_id).received_bytes == 8)
        await self.until(lambda: updated.file_id not in self.uploads.by_file)
        async with self.peer(device="new", with_protocol=True) as (new, send_new, protocol):
            resumed = await self.initialize_upload(new, send_new, request)
            self.assertEqual(8, resumed.transferred_bytes)
            writer = await self.upload_stream(protocol, resumed.file_id, 8, payload[8:])
            writer.write_eof()
            await self.finish_upload(new, send_new, resumed.file_id, payload)

    async def test_other_device_cancel_invalidates_active_upload_stream(self):
        payload = b"cancel cross-device upload"
        request = self.shared_upload_request(payload)
        async with self.peer(device="old", with_protocol=True) as (old, send_old, protocol), self.peer(
                device="new") as (new, send_new):
            updated = await self.initialize_upload(old, send_old, request)
            writer = await self.upload_stream(protocol, updated.file_id, 0, payload[:8])
            await self.until(lambda: self.files.get_transfer_by_file_id(updated.file_id).received_bytes == 8)
            send_new("cancel", file_cancel=file_pb2.FileCancel(client_file_id=request.client_file_id, file_id=updated.file_id))
            self.assertTrue((await self.read(new, "cancel", "ack")).ack.success)
            writer.write(payload[8:])
            await self.rejected_stream(protocol, writer)
            self.assertEqual("cancelled", self.files.get_transfer_by_file_id(updated.file_id).status)
            self.assertEqual(payload[:8], self.files.get_storage_path(updated.file_id).read_bytes())
            self.assertEqual(0, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])


    async def test_invalid_upload_headers_preserve_valid_grant(self):
        payload = b"only the correct stream writes"
        request = self.shared_upload_request(payload)
        async with self.peer(with_protocol=True) as (reader, send, protocol):
            updated = await self.initialize_upload(reader, send, request)
            headers = [f"MINIIMFILE1 {updated.file_id}\n".encode(), b"x" * 513,
                b"MINIIMFILE2 \xff 0\n", f"MINIIMFILE2 {updated.file_id} -1\n".encode(),
                f"MINIIMFILE2 {updated.file_id} 18446744073709551616\n".encode()]
            for header in headers:
                with self.subTest(header=header[:50]):
                    invalid = UploadSender(protocol)
                    invalid.write(header)
                    await self.rejected_stream(protocol, invalid)
                    self.assertEqual(0, self.files.get_transfer_by_file_id(updated.file_id).received_bytes)
            truncated = UploadSender(protocol)
            truncated.write(b"MINIIMFILE2")
            truncated.write_eof()
            # QUIC may omit STOP_SENDING once the peer's FIN was already received.
            await self.until(lambda: ("first-device", truncated.stream_id) in self.rejected_uploads)
            self.assertEqual(0, self.uploads.by_file[updated.file_id].offset)
            writer = UploadSender(protocol)
            writer.write(f"MINIIMFILE2 {updated.file_id} ".encode())
            send("barrier", heartbeat=auth_pb2.Heartbeat())
            await self.read(reader, "barrier", "heartbeat")
            writer.write(b"0\n" + payload)
            writer.write_eof()
            await self.finish_upload(reader, send, updated.file_id, payload)

    async def test_stream_reset_releases_only_its_upload(self):
        payload = b"resume reset stream"
        request = self.shared_upload_request(payload)
        other_request = self.shared_upload_request(b"another intent", intent="other-upload")
        async with self.peer(with_protocol=True) as (reader, send, protocol):
            updated = await self.initialize_upload(reader, send, request)
            other = await self.initialize_upload(reader, send, other_request, "other-init")
            writer = await self.upload_stream(protocol, updated.file_id, 0, payload[:6])
            await self.until(lambda: self.files.get_transfer_by_file_id(updated.file_id).received_bytes == 6)
            protocol._quic.reset_stream(writer.stream_id, 123)
            protocol.transmit()
            await self.until(lambda: updated.file_id not in self.uploads.by_file)
            self.assertIn(other.file_id, self.uploads.by_file)
            resumed = await self.initialize_upload(reader, send, request, "resume")
            self.assertEqual(6, resumed.transferred_bytes)
            writer = await self.upload_stream(protocol, updated.file_id, 6, payload[6:])
            writer.write_eof()
            await self.finish_upload(reader, send, updated.file_id, payload)
            self.assertIn(other.file_id, self.uploads.by_file)

    async def test_zero_idle_timeout_keeps_upload_owned_until_disconnect(self):
        self.uploads.idle_timeout_ms = 0
        request = self.shared_upload_request(b"explicit release")
        async with self.peer(device="old") as (old, send_old), self.peer(device="new") as (new, send_new):
            updated = await self.initialize_upload(old, send_old, request)
            self.uploads.by_file[updated.file_id].touched -= 100000
            send_new("takeover", file_init=request)
            self.assertEqual(429, (await self.read(new, "takeover")).error.code)


    async def test_same_device_new_connection_supersedes_old_connection(self):
        request = self.shared_upload_request(b"same device recovery")
        self.uploads.idle_timeout_ms = 0
        async with self.peer(device="shared", with_protocol=True) as (old, send_old, old_protocol), self.peer(
                device="shared", with_protocol=True) as (new, send_new, new_protocol):
            first = await self.initialize_upload(old, send_old, request)
            old_writer = await self.upload_stream(old_protocol, first.file_id, 0, b"same ")
            await self.until(lambda: self.files.get_transfer_by_file_id(first.file_id).received_bytes == 5)
            resumed = await self.initialize_upload(new, send_new, request)
            self.assertEqual(5, resumed.transferred_bytes)
            send_old("stale-init", file_init=request)
            self.assertEqual(429, (await self.read(old, "stale-init")).error.code)
            old_writer.write(b"obsolete data")
            await self.rejected_stream(old_protocol, old_writer)
            writer = await self.upload_stream(new_protocol, first.file_id, 5, b"device recovery")
            writer.write_eof()
            await self.finish_upload(new, send_new, first.file_id, b"same device recovery")


if __name__ == "__main__":
    unittest.main()
