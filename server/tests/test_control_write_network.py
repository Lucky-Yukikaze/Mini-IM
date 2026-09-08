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

from aioquic.asyncio import connect
from aioquic.quic.configuration import QuicConfiguration
from protocol.codec import EnvelopeCodec
from protocol.pb import common_pb2, conversation_pb2, envelope_pb2, file_pb2
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
        dropped = self.drop_ack

        class FaultProtocol(MiniImQuicProtocol):
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
    async def peer(self, user="alice", device="first-device"):
        config = QuicConfiguration(is_client=True, alpn_protocols=["mini-im"])
        config.verify_mode = ssl.CERT_NONE
        async with connect("127.0.0.1", self.port, configuration=config) as protocol:
            reader, writer = await protocol.create_stream()
            hello = envelope_pb2.Envelope(version=1, request_id="hello", channel=common_pb2.CHANNEL_CONTROL)
            hello.hello.token = "dev-token:" + user
            hello.hello.device_id = device
            writer.write(EnvelopeCodec.encode_frame(hello))
            welcome = await self.read(reader, "hello", "welcome")

            def send(request_id, **body):
                envelope = envelope_pb2.Envelope(
                    version=1, request_id=request_id, channel=common_pb2.CHANNEL_CONTROL,
                    session_id=welcome.welcome.session_id, device_id=device, **body)
                writer.write(EnvelopeCodec.encode_frame(envelope))
            yield reader, send
            writer.close()

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


if __name__ == "__main__":
    unittest.main()
