"""Cancellation persists even before initialization and never unpublishes a file."""
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from protocol.pb import common_pb2, conversation_pb2, file_pb2
from services.conversation.service import ConversationService
from services.file.service import FileService
from storage.repo import ConversationRepo, FileRepo, MessageRepo
from storage.sqlite.db import MiniImSqliteDb
from storage.sqlite.init_db import init_db


class FileCancelTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "test.db"
        init_db(self.path)
        self.db = MiniImSqliteDb(self.path)
        self.build()
        for user in ("alice", "bob", "cindy"):
            self.conversations.ensure_user(user)
        self.conversation = self.conversations.handle_create_conversation("alice", "setup",
            conversation_pb2.CreateConversation(client_conv_id="group", title="group",
                type=common_pb2.CONVERSATION_GROUP, member_ids=["bob"])).ack.entity_id
        self.payload = b"cancellable file content"
        self.request = file_pb2.FileInit(conversation_id=self.conversation, client_file_id="intent",
            file_name="source.bin", file_size=len(self.payload), sha256=hashlib.sha256(self.payload).hexdigest(),
            direction=common_pb2.FILE_DIRECTION_UPLOAD)

    def build(self):
        self.repo = FileRepo(self.db)
        conversation = ConversationRepo(self.db)
        self.conversations = ConversationService(conversation)
        self.files = FileService(self.repo, conversation, MessageRepo(self.db), self.root / "files", 1)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def count(self):
        return self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0]

    def cancel(self, user="alice", request="cancel", intent="intent", file_id=""):
        return self.files.handle_file_cancel(user, request, file_pb2.FileCancel(client_file_id=intent, file_id=file_id))

    def initialize(self):
        result = self.files.handle_file_init("alice", "init", self.request)
        self.assertTrue(result.ack.success)
        return result.ack.entity_id

    def test_cancel_before_init_survives_upgrade_reopen_and_isolates_user(self):
        # The additive schema also upgrades a database with old transfer rows.
        legacy = file_pb2.FileInit()
        legacy.CopyFrom(self.request)
        legacy.client_file_id = "legacy-intent"
        old = self.files.handle_file_init("alice", "legacy-init", legacy)
        self.files.append_file_chunk("alice", old.ack.entity_id, self.payload[:5])
        self.db.execute_write("DROP TABLE file_cancellations")
        self.db.close()
        init_db(self.path)
        self.db = MiniImSqliteDb(self.path)
        self.build()
        self.assertEqual(5, self.repo.get_transfer_by_file_id(old.ack.entity_id).received_bytes)
        self.assertEqual(self.payload[:5], self.files.get_storage_path(old.ack.entity_id).read_bytes())
        result = self.cancel()
        self.assertTrue(result.ack.success)
        self.assertEqual([], result.sync_events)
        self.db.close()
        init_db(self.path)
        self.db = MiniImSqliteDb(self.path)
        self.build()
        duplicate = self.cancel()
        self.assertEqual(result.ack, duplicate.ack)
        self.assertFalse(self.files.handle_file_init("alice", "late", self.request).ack.success)
        self.assertTrue(self.files.handle_file_init("bob", "other-account", self.request).ack.success)

    def test_upload_cancel_replays_exact_ack_and_rejects_late_writes(self):
        file_id = self.initialize()
        self.files.append_file_chunk("alice", file_id, self.payload[:5])
        old = self.repo.get_transfer_by_file_id(file_id)
        result = self.cancel(file_id=file_id)
        self.assertTrue(result.ack.success)
        self.assertEqual(file_id, result.cancelled_file_id)
        self.assertGreater(self.repo.get_transfer_by_file_id(file_id).version, old.version)
        self.assertEqual(2, len(result.sync_events))
        count = self.count()
        for retry in ("cancel", "new-cancel"):
            duplicate = self.cancel(request=retry, file_id=file_id)
            self.assertTrue(duplicate.ack.success)
            self.assertEqual([], duplicate.sync_events)
        self.assertEqual((None, []), self.files.append_file_chunk("alice", file_id, self.payload[5:]))
        self.assertFalse(self.files.handle_file_init("alice", "late-init", self.request).ack.success)
        for success in (True, False):
            self.assertFalse(self.files.handle_file_finish("alice", "late-finish",
                file_pb2.FileFinish(file_id=file_id, success=success)).ack.success)
        self.repo.apply_finish(file_id, True, ["alice", "bob"])
        self.repo.apply_progress(file_id, len(self.payload), ["alice", "bob"])
        self.assertEqual("cancelled", self.repo.get_transfer_by_file_id(file_id).status)
        self.assertEqual(count, self.count())
        self.assertEqual(self.payload[:5], self.files.get_storage_path(file_id).read_bytes())

    def test_completed_upload_wins_and_cancel_failure_is_stable(self):
        file_id = self.initialize()
        self.files.append_file_chunk("alice", file_id, self.payload)
        self.assertTrue(self.files.handle_file_finish("alice", "finish",
            file_pb2.FileFinish(file_id=file_id, success=True)).ack.success)
        count = self.count()
        result = self.cancel(file_id=file_id)
        self.assertFalse(result.ack.success)
        self.assertEqual(409, result.ack.code)
        self.assertEqual(result.ack, self.cancel(file_id=file_id).ack)
        self.assertEqual("completed", self.repo.get_transfer_by_file_id(file_id).status)
        self.assertFalse(self.repo.is_cancelled("alice", "intent"))
        self.assertEqual(count, self.count())
        self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])
        self.assertEqual(self.payload, self.files.get_storage_path(file_id).read_bytes())

    def test_owner_can_cancel_after_leaving_but_cannot_cancel_another_owner(self):
        file_id = self.initialize()
        self.assertEqual(403, self.cancel(user="bob", file_id=file_id).ack.code)
        self.assertFalse(self.repo.is_cancelled("bob", "intent"))
        self.conversations.handle_leave_conversation("alice", "leave",
            conversation_pb2.LeaveConversation(conversation_id=self.conversation))
        result = self.cancel(file_id=file_id)
        self.assertTrue(result.ack.success)
        self.assertEqual({"alice", "bob"}, {event.user_id for event in result.sync_events})

    def test_cancel_request_cannot_change_intent_or_file_reference(self):
        file_id = self.initialize()
        self.assertTrue(self.cancel(file_id=file_id).ack.success)
        self.assertEqual(409, self.cancel(intent="changed", file_id=file_id).ack.code)
        self.assertEqual(409, self.cancel(file_id="changed").ack.code)
        self.assertEqual(400, self.cancel(request="empty", intent="").ack.code)
        self.assertFalse(self.repo.is_cancelled("alice", "changed"))

    def test_cancel_result_failure_rolls_back_state_events_and_tombstone(self):
        file_id = self.initialize()
        before = self.repo.get_transfer_by_file_id(file_id)
        count = self.count()
        self.db.execute_write("CREATE TRIGGER reject_cancel_result BEFORE INSERT ON control_write_results "
            "BEGIN SELECT RAISE(ABORT,'injected cancel commit failure'); END")
        with self.assertRaises(sqlite3.Error):
            self.cancel(file_id=file_id)
        self.assertFalse(self.db.m_connection.in_transaction)
        self.assertFalse(self.repo.is_cancelled("alice", "intent"))
        self.assertEqual(before, self.repo.get_transfer_by_file_id(file_id))
        self.assertEqual(count, self.count())
        self.db.execute_write("DROP TRIGGER reject_cancel_result")
        self.assertTrue(self.cancel(file_id=file_id).ack.success)

    def test_download_cancel_preserves_source_and_rejects_late_receiver_confirmation(self):
        file_id = self.initialize()
        self.files.append_file_chunk("alice", file_id, self.payload)
        self.files.handle_file_finish("alice", "finish", file_pb2.FileFinish(file_id=file_id, success=True))
        download = self.files.handle_file_init("bob", "download", file_pb2.FileInit(
            client_file_id="download", source_file_id=file_id, direction=common_pb2.FILE_DIRECTION_DOWNLOAD))
        self.assertTrue(self.cancel(user="bob", intent="download", file_id=download.ack.entity_id).ack.success)
        result = self.files.handle_file_finish("bob", "late-finish", file_pb2.FileFinish(
            file_id=download.ack.entity_id, success=True, transferred_bytes=len(self.payload), sha256=self.request.sha256))
        self.assertEqual(409, result.ack.code)
        self.assertEqual("completed", self.repo.get_transfer_by_file_id(file_id).status)
        self.assertEqual(self.payload, self.files.get_storage_path(file_id).read_bytes())


if __name__ == "__main__":
    unittest.main()
