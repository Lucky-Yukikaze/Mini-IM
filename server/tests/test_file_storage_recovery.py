"""Recover upload progress only from bytes present within the committed prefix."""
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from protocol.pb import common_pb2, conversation_pb2, file_pb2
from services.conversation.service import ConversationService
from services.file.service import FileService
from storage.repo import ConversationRepo, FileRepo, MessageRepo
from storage.sqlite.db import MiniImSqliteDb


class FileStorageRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = MiniImSqliteDb(self.root / "test.db")
        self.db.init_schema()
        self.build_services()
        for user in ("alice", "bob"):
            self.conversations.ensure_user(user)
        created = self.conversations.handle_create_conversation("alice", "setup",
            conversation_pb2.CreateConversation(client_conv_id="group", title="group",
                type=common_pb2.CONVERSATION_GROUP, member_ids=["bob"]))
        self.conversation = created.ack.entity_id
        self.payload = b"persisted upload prefix and a verified tail"
        self.request = file_pb2.FileInit(conversation_id=self.conversation, client_file_id="upload",
            file_name="source.bin", file_size=len(self.payload), sha256=hashlib.sha256(self.payload).hexdigest(),
            direction=common_pb2.FILE_DIRECTION_UPLOAD)
        first = self.files.handle_file_init("alice", "init", self.request)
        self.assertTrue(first.ack.success)
        self.file_id = first.ack.entity_id
        self.path = self.files.get_storage_path(self.file_id)

    def build_services(self):
        conversations = ConversationRepo(self.db)
        self.repo = FileRepo(self.db)
        self.conversations = ConversationService(conversations)
        self.files = FileService(self.repo, conversations, MessageRepo(self.db), self.root / "files", 1000)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def resume(self):
        result = self.files.handle_file_init("alice", "init", self.request)
        self.assertTrue(result.ack.success)
        self.assertEqual(self.file_id, result.ack.entity_id)
        return result

    def complete(self, offset, request_id="finish"):
        self.files.append_file_chunk("alice", self.file_id, self.payload[offset:])
        result = self.files.handle_file_finish("alice", request_id,
            file_pb2.FileFinish(file_id=self.file_id, success=True))
        self.assertTrue(result.ack.success, result.ack.message)
        self.assertTrue(result.file_updated.completed)
        self.assertEqual(self.payload, self.path.read_bytes())
        self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])

    def test_truncated_stale_upload_reopens_at_existing_prefix_with_stable_events(self):
        self.files.append_file_chunk("alice", self.file_id, self.payload[:16])
        self.path.write_bytes(self.payload[:5])
        old = self.repo.get_transfer_by_file_id(self.file_id)
        self.db.execute_write("UPDATE file_transfers SET updated_at_ms=0 WHERE file_id=?", (self.file_id,))
        self.db.close()
        self.db = MiniImSqliteDb(self.root / "test.db")
        self.db.init_schema()
        self.build_services()
        result = self.resume()
        self.assertEqual(5, result.file_updated.transferred_bytes)
        self.assertGreater(result.file_updated.version, old.version)
        own = [event for event in result.sync_events if event.user_id == "alice"]
        self.assertTrue(own)
        last = file_pb2.FileUpdated.FromString(own[-1].payload)
        self.assertEqual(result.file_updated, last)
        self.assertEqual(own[-1].event_id, last.event_id)
        self.assertEqual([], self.resume().sync_events)
        self.complete(5)

    def test_missing_upload_resumes_from_zero(self):
        self.files.append_file_chunk("alice", self.file_id, self.payload[:16])
        self.path.unlink()
        result = self.resume()
        self.assertEqual(0, result.file_updated.transferred_bytes)
        self.complete(0)

    def test_uncommitted_disk_tail_is_discarded_not_promoted(self):
        self.db.execute_write("CREATE TRIGGER reject_progress BEFORE UPDATE ON file_transfers "
            "BEGIN SELECT RAISE(ABORT, 'injected progress failure'); END")
        with self.assertRaises(sqlite3.Error):
            self.files.append_file_chunk("alice", self.file_id, self.payload[:16])
        self.assertEqual(0, self.repo.get_transfer_by_file_id(self.file_id).received_bytes)
        self.assertFalse(self.db.m_connection.in_transaction)
        self.assertEqual(self.payload[:16], self.path.read_bytes())
        self.db.execute_write("DROP TRIGGER reject_progress")
        self.resume()
        self.assertEqual(b"", self.path.read_bytes())
        self.complete(0)

    def test_partial_writes_are_completed_before_progress_is_committed(self):
        original = Path.open
        def short_open(path, *args, **kwargs):
            file = original(path, *args, **kwargs)
            if path == self.path and ("+" in args[0] or "w" in args[0]):
                write = file.write
                file.write = lambda data: write(data[:3])
            return file
        with patch.object(Path, "open", short_open):
            self.files.append_file_chunk("alice", self.file_id, self.payload[:16])
        self.assertEqual(self.payload[:16], self.path.read_bytes())
        self.assertEqual(16, self.repo.get_transfer_by_file_id(self.file_id).received_bytes)
        self.complete(16)

    def test_zero_write_and_sync_failure_do_not_advance_progress(self):
        original = Path.open
        def zero_open(path, *args, **kwargs):
            file = original(path, *args, **kwargs)
            if path == self.path and ("+" in args[0] or "w" in args[0]):
                file.write = lambda data: 0
            return file
        with patch.object(Path, "open", zero_open), self.assertRaises(OSError):
            self.files.append_file_chunk("alice", self.file_id, self.payload)
        with patch("os.fsync", side_effect=OSError("injected flush failure")), self.assertRaises(OSError):
            self.files.append_file_chunk("alice", self.file_id, self.payload)
        self.assertEqual(0, self.repo.get_transfer_by_file_id(self.file_id).received_bytes)
        self.assertEqual(0, self.resume().file_updated.transferred_bytes)
        self.complete(0)

    def test_bad_digest_can_retry_original_intent_without_overwriting_completed_files(self):
        self.files.append_file_chunk("alice", self.file_id, b"x" * len(self.payload))
        result = self.files.handle_file_finish("alice", "finish",
            file_pb2.FileFinish(file_id=self.file_id, success=True))
        self.assertFalse(result.ack.success)
        self.assertEqual(409, result.ack.code)
        self.assertEqual("failed_integrity", self.repo.get_transfer_by_file_id(self.file_id).status)
        self.assertEqual(0, self.repo.get_transfer_by_file_id(self.file_id).received_bytes)
        self.assertEqual((None, []), self.files.append_file_chunk("alice", self.file_id, b"late bytes"))
        self.assertEqual(0, self.resume().file_updated.transferred_bytes)
        self.complete(0, "finish-repaired")
        finished = self.repo.get_transfer_by_file_id(self.file_id)
        self.path.write_bytes(b"externally changed completed file")
        self.assertTrue(self.resume().file_updated.completed)
        self.assertEqual(finished, self.repo.get_transfer_by_file_id(self.file_id))
        self.assertEqual(b"externally changed completed file", self.path.read_bytes())

    def test_unavailable_published_file_retries_after_restore_without_changing_publication(self):
        self.complete(0)
        publication = self.repo.get_transfer_by_file_id(self.file_id)
        download = file_pb2.FileInit(conversation_id=self.conversation, client_file_id="download",
            source_file_id=self.file_id, direction=common_pb2.FILE_DIRECTION_DOWNLOAD)
        event_count = self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0]
        for content in (None, self.payload[:-1], self.payload + b"extra"):
            with self.subTest(content=content):
                if content is None:
                    self.path.unlink()
                else:
                    self.path.write_bytes(content)
                rejected = self.files.handle_file_init("bob", "download-init", download)
                self.assertFalse(rejected.ack.success)
                self.assertEqual(409, rejected.ack.code)
                self.assertFalse(rejected.start_download)
                self.assertEqual(publication, self.repo.get_transfer_by_file_id(self.file_id))
                self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM file_transfers")[0])
                self.assertEqual(event_count, self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0])
        denied = self.files.handle_file_init("outsider", "download-init", download)
        self.assertEqual(403, denied.ack.code)
        self.path.write_bytes(self.payload)
        restored = self.files.handle_file_init("bob", "download-init", download)
        self.assertTrue(restored.ack.success)
        repeated = self.files.handle_file_init("bob", "download-init", download)
        self.assertEqual(restored.ack.entity_id, repeated.ack.entity_id)
        self.assertEqual(publication, self.repo.get_transfer_by_file_id(self.file_id))
        self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])

    def test_repair_failure_does_not_publish_progress_or_lose_retry(self):
        self.files.append_file_chunk("alice", self.file_id, self.payload[:16])
        self.path.write_bytes(self.payload[:5])
        old = self.repo.get_transfer_by_file_id(self.file_id)
        count = self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0]
        self.db.execute_write("CREATE TRIGGER reject_repair BEFORE INSERT ON sync_events "
            "BEGIN SELECT RAISE(ABORT, 'injected event failure'); END")
        with self.assertRaises(sqlite3.Error):
            self.resume()
        self.assertEqual(old, self.repo.get_transfer_by_file_id(self.file_id))
        self.assertEqual(count, self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0])
        self.db.execute_write("DROP TRIGGER reject_repair")
        self.assertEqual(5, self.resume().file_updated.transferred_bytes)
        self.complete(5)


if __name__ == "__main__":
    unittest.main()
