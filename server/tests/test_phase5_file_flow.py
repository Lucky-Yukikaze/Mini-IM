import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from protocol.pb import common_pb2, conversation_pb2, file_pb2, sync_pb2
from services.conversation.service import ConversationService
from services.file.service import FileService
from services.sync.service import SyncService
from storage.repo import ConversationRepo, FileRepo, MessageRepo, SyncRepo
from storage.sqlite.db import MiniImSqliteDb


class Phase5FileFlowTest(unittest.TestCase):
    def _build_services(self, db: MiniImSqliteDb, file_root: Path) -> tuple[ConversationService, FileService, SyncService]:
        conversation_repo = ConversationRepo(db)
        message_repo = MessageRepo(db)
        conversation_service = ConversationService(conversation_repo)
        file_service = FileService(
            FileRepo(db),
            conversation_repo,
            message_repo,
            file_root=file_root,
            stale_timeout_ms=1000,
        )
        sync_service = SyncService(SyncRepo(db))
        return conversation_service, file_service, sync_service

    def test_file_upload_flow_can_sync_file_updated(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            file_root = Path(tmp) / "files"
            db = MiniImSqliteDb(db_path)
            db.init_schema()

            conversation_service, file_service, sync_service = self._build_services(db, file_root)
            create_result = conversation_service.handle_create_conversation(
                user_id="u-alice",
                request_id="req-create",
                create_conversation=conversation_pb2.CreateConversation(
                    client_conv_id="cc-1",
                    type=common_pb2.CONVERSATION_GROUP,
                    member_ids=["u-bob"],
                    title="group-1",
                ),
            )
            self.assertTrue(create_result.ack.success)
            conversation_id = create_result.ack.entity_id

            payload = b"hello-file-phase5"
            sha256 = hashlib.sha256(payload).hexdigest()

            init_result = file_service.handle_file_init(
                user_id="u-alice",
                request_id="req-file-init",
                file_init=file_pb2.FileInit(
                    conversation_id=conversation_id,
                    client_file_id="intent-1",
                    file_name="demo.txt",
                    file_size=len(payload),
                    sha256=sha256,
                    direction=common_pb2.FILE_DIRECTION_UPLOAD,
                    resume_offset=0,
                    priority=1,
                ),
            )
            self.assertTrue(init_result.ack.success)
            self.assertIsNotNone(init_result.file_updated)
            self.assertGreaterEqual(init_result.file_updated.version, 1)
            file_id = init_result.ack.entity_id
            self.assertTrue(file_id)

            updated, sync_events = file_service.append_file_chunk("u-alice", file_id, payload)
            self.assertIsNotNone(updated)
            self.assertGreaterEqual(len(sync_events), 1)

            finish_result = file_service.handle_file_finish(
                user_id="u-alice",
                request_id="req-file-finish",
                file_finish=file_pb2.FileFinish(file_id=file_id, success=True),
            )
            self.assertTrue(finish_result.ack.success)
            self.assertIsNotNone(finish_result.file_updated)
            self.assertTrue(finish_result.file_updated.completed)

            alice_sync, _ = sync_service.handle_sync_request(
                user_id="u-alice",
                request=sync_pb2.SyncRequest(global_cursor=0, limit=100),
            )
            bob_sync, _ = sync_service.handle_sync_request(
                user_id="u-bob",
                request=sync_pb2.SyncRequest(global_cursor=0, limit=100),
            )
            self.assertTrue(any(item.HasField("file_updated") for item in alice_sync.events))
            self.assertTrue(any(item.HasField("file_updated") for item in bob_sync.events))
            self.assertTrue(any(item.HasField("message") for item in alice_sync.events))
            file_msg = next(item.message for item in alice_sync.events if item.HasField("message") and item.message.type == common_pb2.MSG_FILE)
            content = json.loads(file_msg.content.decode("utf-8"))
            self.assertEqual(file_id, content["fileId"])
            db.close()

    def test_file_finish_should_reject_if_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            file_root = Path(tmp) / "files"
            db = MiniImSqliteDb(db_path)
            db.init_schema()

            conversation_service, file_service, _ = self._build_services(db, file_root)
            create_result = conversation_service.handle_create_conversation(
                user_id="u-alice",
                request_id="req-create",
                create_conversation=conversation_pb2.CreateConversation(
                    client_conv_id="cc-1",
                    type=common_pb2.CONVERSATION_GROUP,
                    member_ids=["u-bob"],
                    title="group-1",
                ),
            )
            conversation_id = create_result.ack.entity_id
            payload = b"abc123"
            sha256 = hashlib.sha256(payload).hexdigest()

            init_result = file_service.handle_file_init(
                user_id="u-alice",
                request_id="req-init",
                file_init=file_pb2.FileInit(
                    conversation_id=conversation_id,
                    client_file_id="intent-1",
                    file_name="x.bin",
                    file_size=len(payload),
                    sha256=sha256,
                    direction=common_pb2.FILE_DIRECTION_UPLOAD,
                    resume_offset=0,
                    priority=0,
                ),
            )
            file_id = init_result.ack.entity_id
            file_service.append_file_chunk("u-alice", file_id, payload[:3])
            finish_result = file_service.handle_file_finish(
                user_id="u-alice",
                request_id="req-finish",
                file_finish=file_pb2.FileFinish(file_id=file_id, success=True),
            )
            self.assertFalse(finish_result.ack.success)
            self.assertEqual(400, finish_result.ack.code)
            db.close()

    def test_same_intent_id_with_different_file_size_should_be_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            file_root = Path(tmp) / "files"
            db = MiniImSqliteDb(db_path)
            db.init_schema()

            conversation_service, file_service, _ = self._build_services(db, file_root)
            create_result = conversation_service.handle_create_conversation(
                user_id="u-alice",
                request_id="req-create",
                create_conversation=conversation_pb2.CreateConversation(
                    client_conv_id="cc-1",
                    type=common_pb2.CONVERSATION_GROUP,
                    member_ids=["u-bob"],
                    title="group-1",
                ),
            )
            conversation_id = create_result.ack.entity_id
            intent_id = "intent-1"

            first = file_service.handle_file_init(
                user_id="u-alice",
                request_id="req-file-init-1",
                file_init=file_pb2.FileInit(
                    conversation_id=conversation_id,
                    client_file_id=intent_id,
                    file_name="a.bin",
                    file_size=10,
                    sha256="x" * 64,
                    direction=common_pb2.FILE_DIRECTION_UPLOAD,
                    resume_offset=0,
                    priority=0,
                ),
            )
            self.assertTrue(first.ack.success)

            second = file_service.handle_file_init(
                user_id="u-alice",
                request_id="req-file-init-2",
                file_init=file_pb2.FileInit(
                    conversation_id=conversation_id,
                    client_file_id=intent_id,
                    file_name="b.bin",
                    file_size=11,
                    sha256="y" * 64,
                    direction=common_pb2.FILE_DIRECTION_UPLOAD,
                    resume_offset=0,
                    priority=0,
                ),
            )
            self.assertFalse(second.ack.success)
            self.assertEqual(409, second.ack.code)
            db.close()

    def test_stale_intent_can_be_taken_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            file_root = Path(tmp) / "files"
            db = MiniImSqliteDb(db_path)
            db.init_schema()

            conversation_service, file_service, _ = self._build_services(db, file_root)
            create_result = conversation_service.handle_create_conversation(
                user_id="u-alice",
                request_id="req-create",
                create_conversation=conversation_pb2.CreateConversation(
                    client_conv_id="cc-1",
                    type=common_pb2.CONVERSATION_GROUP,
                    member_ids=["u-bob"],
                    title="group-1",
                ),
            )
            conversation_id = create_result.ack.entity_id
            payload = b"abcdef"
            sha256 = hashlib.sha256(payload).hexdigest()

            first = file_service.handle_file_init(
                user_id="u-alice",
                request_id="req-file-init-1",
                file_init=file_pb2.FileInit(
                    conversation_id=conversation_id,
                    client_file_id="intent-stale",
                    file_name="a.bin",
                    file_size=len(payload),
                    sha256=sha256,
                    direction=common_pb2.FILE_DIRECTION_UPLOAD,
                    resume_offset=0,
                    priority=0,
                ),
            )
            file_id = first.ack.entity_id
            file_service.append_file_chunk("u-alice", file_id, payload[:2])

            db.execute_write(
                "UPDATE file_transfers SET updated_at_ms = updated_at_ms - 5000, status = 'uploading' WHERE file_id = ?",
                (file_id,),
            )

            takeover = file_service.handle_file_init(
                user_id="u-alice",
                request_id="req-file-init-2",
                file_init=file_pb2.FileInit(
                    conversation_id=conversation_id,
                    client_file_id="intent-stale",
                    file_name="a.bin",
                    file_size=len(payload),
                    sha256=sha256,
                    direction=common_pb2.FILE_DIRECTION_UPLOAD,
                    resume_offset=2,
                    priority=0,
                ),
            )
            self.assertTrue(takeover.ack.success)
            self.assertEqual(file_id, takeover.ack.entity_id)
            self.assertGreaterEqual(takeover.file_updated.version, first.file_updated.version)
            db.close()

    def test_download_direction_can_resume_and_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            file_root = Path(tmp) / "files"
            db = MiniImSqliteDb(db_path)
            db.init_schema()

            conversation_service, file_service, sync_service = self._build_services(db, file_root)
            create_result = conversation_service.handle_create_conversation(
                user_id="u-alice",
                request_id="req-create",
                create_conversation=conversation_pb2.CreateConversation(
                    client_conv_id="cc-1",
                    type=common_pb2.CONVERSATION_GROUP,
                    member_ids=["u-bob"],
                    title="group-1",
                ),
            )
            conversation_id = create_result.ack.entity_id
            payload = b"download-source-data"
            sha256 = hashlib.sha256(payload).hexdigest()

            up_init = file_service.handle_file_init(
                user_id="u-alice",
                request_id="req-up-init",
                file_init=file_pb2.FileInit(
                    conversation_id=conversation_id,
                    client_file_id="intent-up",
                    file_name="src.bin",
                    file_size=len(payload),
                    sha256=sha256,
                    direction=common_pb2.FILE_DIRECTION_UPLOAD,
                    resume_offset=0,
                    priority=0,
                ),
            )
            file_service.append_file_chunk("u-alice", up_init.ack.entity_id, payload)
            up_finish = file_service.handle_file_finish(
                user_id="u-alice",
                request_id="req-up-finish",
                file_finish=file_pb2.FileFinish(file_id=up_init.ack.entity_id, success=True),
            )
            self.assertTrue(up_finish.ack.success)

            dl_init = file_service.handle_file_init(
                user_id="u-bob",
                request_id="req-dl-init",
                file_init=file_pb2.FileInit(
                    conversation_id=conversation_id,
                    client_file_id="intent-dl",
                    file_name="ignored.bin",
                    file_size=1,
                    sha256="na",
                    direction=common_pb2.FILE_DIRECTION_DOWNLOAD,
                    resume_offset=5,
                    priority=0,
                    source_file_id=up_init.ack.entity_id,
                ),
            )
            self.assertTrue(dl_init.ack.success)
            self.assertTrue(dl_init.start_download)
            dl_file_id = dl_init.download_file_id
            self.assertTrue(dl_file_id)

            updated, _ = file_service.apply_download_progress("u-bob", dl_file_id, 10)
            self.assertIsNotNone(updated)
            done, sync_events = file_service.complete_download("u-bob", dl_file_id)
            self.assertIsNotNone(done)
            self.assertTrue(done.completed)
            self.assertGreaterEqual(len(sync_events), 1)

            bob_sync, _ = sync_service.handle_sync_request(
                user_id="u-bob",
                request=sync_pb2.SyncRequest(global_cursor=0, limit=100),
            )
            self.assertTrue(any(item.HasField("file_updated") for item in bob_sync.events))
            db.close()

    def test_large_file_upload_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            file_root = Path(tmp) / "files"
            db = MiniImSqliteDb(db_path)
            db.init_schema()

            conversation_service, file_service, _ = self._build_services(db, file_root)
            create_result = conversation_service.handle_create_conversation(
                user_id="u-alice",
                request_id="req-create",
                create_conversation=conversation_pb2.CreateConversation(
                    client_conv_id="cc-1",
                    type=common_pb2.CONVERSATION_GROUP,
                    member_ids=["u-bob"],
                    title="group-1",
                ),
            )
            conversation_id = create_result.ack.entity_id

            payload = b"A" * (2 * 1024 * 1024)
            sha256 = hashlib.sha256(payload).hexdigest()
            init_result = file_service.handle_file_init(
                user_id="u-alice",
                request_id="req-large-init",
                file_init=file_pb2.FileInit(
                    conversation_id=conversation_id,
                    client_file_id="intent-large",
                    file_name="large.bin",
                    file_size=len(payload),
                    sha256=sha256,
                    direction=common_pb2.FILE_DIRECTION_UPLOAD,
                    resume_offset=0,
                    priority=0,
                ),
            )
            file_id = init_result.ack.entity_id
            first_half = payload[: len(payload) // 2]
            second_half = payload[len(payload) // 2 :]
            file_service.append_file_chunk("u-alice", file_id, first_half)

            db.execute_write(
                "UPDATE file_transfers SET updated_at_ms = updated_at_ms - 5000, status = 'uploading' WHERE file_id = ?",
                (file_id,),
            )
            takeover = file_service.handle_file_init(
                user_id="u-alice",
                request_id="req-large-recover",
                file_init=file_pb2.FileInit(
                    conversation_id=conversation_id,
                    client_file_id="intent-large",
                    file_name="large.bin",
                    file_size=len(payload),
                    sha256=sha256,
                    direction=common_pb2.FILE_DIRECTION_UPLOAD,
                    resume_offset=len(first_half),
                    priority=0,
                ),
            )
            self.assertTrue(takeover.ack.success)
            file_service.append_file_chunk("u-alice", file_id, second_half)
            finish = file_service.handle_file_finish(
                user_id="u-alice",
                request_id="req-large-finish",
                file_finish=file_pb2.FileFinish(file_id=file_id, success=True),
            )
            self.assertTrue(finish.ack.success)
            self.assertTrue(finish.file_updated.completed)
            db.close()


if __name__ == "__main__":
    unittest.main()
