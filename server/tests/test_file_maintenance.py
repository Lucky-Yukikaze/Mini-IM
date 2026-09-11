"""Maintenance must preserve published identities and never remove recoverable uploads."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from protocol.pb import common_pb2, conversation_pb2, file_pb2
from services.conversation.service import ConversationService
from services.file.service import FileService
from services.file.maintenance import FileMaintenance
from storage.access import storage_access
from storage.repo import ConversationRepo, FileRepo, MessageRepo
from storage.sqlite.db import MiniImSqliteDb
from storage.sqlite.init_db import init_db


class FileMaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.file_root = self.data / "storage/files"
        self.database = self.data / "storage/sqlite/miniim.db"
        self.database.parent.mkdir(parents=True)
        init_db(self.database)
        self.db = MiniImSqliteDb(self.database)
        self.addCleanup(self.db.close)
        repo = ConversationRepo(self.db)
        conversations = ConversationService(repo)
        for user in ("alice", "bob"):
            conversations.ensure_user(user)
        self.conversation = conversations.handle_create_conversation("alice", "group",
            conversation_pb2.CreateConversation(client_conv_id="group", type=common_pb2.CONVERSATION_GROUP,
                title="maintenance", member_ids=["bob"])).ack.entity_id
        self.files = FileService(FileRepo(self.db), repo, MessageRepo(self.db), self.file_root, 1000)
        self.payload = b"original published content" * 100
        self.replacement = self.root / "backup.bin"
        self.replacement.write_bytes(self.payload)

    def create(self, intent, *, completed=False, cancelled=False):
        result = self.files.handle_file_init("alice", "init-" + intent, file_pb2.FileInit(
            client_file_id=intent, conversation_id=self.conversation, file_name="file.bin",
            file_size=len(self.payload), sha256=hashlib.sha256(self.payload).hexdigest(), direction=1))
        self.assertTrue(result.ack.success)
        fid = result.ack.entity_id
        self.files.append_file_chunk("alice", fid, self.payload if completed else self.payload[:20])
        if completed:
            self.assertTrue(self.files.handle_file_finish("alice", "finish-" + intent,
                file_pb2.FileFinish(file_id=fid, success=True)).ack.success)
        if cancelled:
            self.assertTrue(self.files.handle_file_cancel("alice", "cancel-" + intent,
                file_pb2.FileCancel(file_id=fid, client_file_id=intent)).ack.success)
        return fid, self.files.get_storage_path(fid)

    def maintenance(self):
        return FileMaintenance(self.data, self.file_root)

    def snapshot(self):
        return list(self.db.m_connection.iterdump())

    def age(self, fid):
        self.db.execute_write("UPDATE file_transfers SET updated_at_ms=0 WHERE file_id=?", (fid,))
        self.db.execute_write("UPDATE file_cancellations SET created_at_ms=0 WHERE file_id=?", (fid,))

    def test_inspect_and_restore_preserve_database_and_original_download_identity(self):
        fid, path = self.create("published", completed=True)
        before = self.snapshot()
        for content in (None, b"short", b"x" * len(self.payload)):
            with self.subTest(content=content):
                if content is None:
                    path.unlink()
                else:
                    path.write_bytes(content)
                self.assertIn(self.maintenance().inspect(fid)[0]["health"], ("missing", "damaged"))
                plan = self.maintenance().restore(fid, self.replacement)
                self.assertFalse(plan["applied"])
                self.assertEqual(content, path.read_bytes() if path.exists() else None)
                result = self.maintenance().restore(fid, self.replacement, apply=True)
                self.assertTrue(result["applied"])
                self.assertEqual(self.payload, path.read_bytes())
                self.assertEqual("ok", self.maintenance().inspect(fid)[0]["health"])
                self.assertEqual(before, self.snapshot())
        self.assertEqual("already-valid", self.maintenance().restore(fid, self.replacement, apply=True)["action"])
        download = file_pb2.FileInit(client_file_id="download", source_file_id=fid, direction=2)
        first = self.files.handle_file_init("bob", "download-init", download)
        again = self.files.handle_file_init("bob", "download-init", download)
        self.assertTrue(first.ack.success)
        self.assertEqual(first.ack.entity_id, again.ack.entity_id)
        self.assertEqual(1, self.db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])

    def test_wrong_or_changing_replacement_and_disk_error_preserve_target(self):
        fid, path = self.create("published", completed=True)
        path.write_bytes(b"damaged")
        self.replacement.write_bytes(b"x" * len(self.payload))
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.maintenance().restore(fid, self.replacement, apply=True)
        self.replacement.write_bytes(self.payload)
        with patch("services.file.maintenance.shutil.copyfileobj", side_effect=lambda source, dest, **kw: dest.write(b"changed")):
            with self.assertRaisesRegex(ValueError, "changed"):
                self.maintenance().restore(fid, self.replacement, apply=True)
        with patch("services.file.maintenance.os.fsync", side_effect=OSError("disk fault")):
            with self.assertRaises(OSError):
                self.maintenance().restore(fid, self.replacement, apply=True)
        self.assertEqual(b"damaged", path.read_bytes())
        self.assertFalse(list(self.file_root.rglob(".miniim-restore-*")))

    def test_cleanup_only_aged_cancelled_bytes_and_preserves_retry_rejection(self):
        old, old_path = self.create("old", cancelled=True)
        recent, recent_path = self.create("recent", cancelled=True)
        active, active_path = self.create("active")
        published, published_path = self.create("published", completed=True)
        self.age(old)
        before = self.snapshot()
        plan = self.maintenance().clean_cancelled(7)
        self.assertTrue(old_path.exists())
        self.assertEqual({"remove-cancelled", "keep"}, {row["action"] for row in plan})
        applied = self.maintenance().clean_cancelled(7, apply=True)
        self.assertEqual([old], [row["fileId"] for row in applied if row["applied"]])
        self.assertFalse(old_path.exists())
        for path in (recent_path, active_path, published_path):
            self.assertTrue(path.exists())
        self.assertEqual(before, self.snapshot())
        self.assertEqual("already-absent", next(row for row in self.maintenance().clean_cancelled(7, apply=True) if row["fileId"] == old)["action"])
        rejected = self.files.handle_file_init("alice", "late-init", file_pb2.FileInit(client_file_id="old", direction=1))
        self.assertEqual(409, rejected.ack.code)

    def test_published_and_shared_references_block_cleanup(self):
        fid, path = self.create("published", completed=True)
        # Inconsistent legacy state must not authorize deleting a published attachment.
        self.db.execute_write("UPDATE file_transfers SET status='cancelled',updated_at_ms=0 WHERE file_id=?", (fid,))
        self.db.execute_write("INSERT INTO file_cancellations VALUES('alice','published',?,0)", (fid,))
        self.assertEqual("published reference", self.maintenance().clean_cancelled(0, apply=True)[0]["reason"])
        self.assertTrue(path.exists())
        old, old_path = self.create("shared", cancelled=True)
        self.age(old)
        other, _ = self.create("other")
        self.db.execute_write("UPDATE file_transfers SET source_file_id=? WHERE file_id=?", (old, other))
        plan = self.maintenance().clean_cancelled(0, apply=True)
        self.assertEqual("transfer reference", next(row for row in plan if row["fileId"] == old)["reason"])
        self.assertTrue(old_path.exists())

    def test_unsafe_paths_and_unfinished_restoration_are_rejected(self):
        fid, path = self.create("cancelled", cancelled=True)
        self.age(fid)
        with self.assertRaisesRegex(ValueError, "completed"):
            self.maintenance().restore(fid, self.replacement, apply=True)
        for relative in ("../backup.bin", "/outside", "C:/outside", ".miniim-storage.lock", "a/../../outside"):
            self.db.execute_write("UPDATE file_transfers SET storage_path=? WHERE file_id=?", (relative, fid))
            with self.assertRaises(ValueError):
                self.maintenance().clean_cancelled(0, apply=True)
        self.assertTrue(path.exists())
        self.assertEqual(self.payload, self.replacement.read_bytes())

    def test_cleanup_cli_preview_apply_repeat_and_negative_retention(self):
        fid, path = self.create("cli-cancelled", cancelled=True)
        self.age(fid)
        before = self.snapshot()
        tool = Path(__file__).resolve().parents[2] / "tools/maintain_files.py"
        def command(*arguments):
            result = subprocess.run([sys.executable, str(tool), "--data-root", str(self.data),
                "clean-cancelled", *arguments], capture_output=True, text=True, encoding="utf-8", timeout=10)
            return result.returncode, json.loads(result.stdout)
        code, plan = command("--older-than-days", "7")
        self.assertEqual(0, code)
        self.assertFalse(plan["result"][0]["applied"])
        self.assertTrue(path.exists())
        self.assertEqual(1, command("--older-than-days", "-1", "--apply")[0])
        self.assertTrue(path.exists())
        code, applied = command("--older-than-days", "7", "--apply")
        self.assertEqual(0, code)
        self.assertTrue(applied["result"][0]["applied"])
        self.assertFalse(path.exists())
        self.assertEqual("already-absent", command("--older-than-days", "7", "--apply")[1]["result"][0]["action"])
        self.assertEqual(before, self.snapshot())

    def test_attachment_path_and_missing_cancel_record_prevent_removal(self):
        published, _ = self.create("published", completed=True)
        fid, path = self.create("attachment", cancelled=True)
        self.age(fid)
        message = self.db.execute_fetchone("SELECT server_msg_id FROM messages")[0]
        relative = str(path.relative_to(self.file_root)).replace("/", "\\")
        self.db.execute_write("INSERT INTO attachments VALUES('attachment',?,'file.bin',20,'digest',?,'ready',0,0)",
            (message, relative))
        self.assertEqual("published reference", next(row for row in self.maintenance().clean_cancelled(0, apply=True)
            if row["fileId"] == fid)["reason"])
        self.assertTrue(path.exists())
        self.db.execute_write("DELETE FROM attachments")
        self.db.execute_write("DELETE FROM file_cancellations WHERE file_id=?", (fid,))
        self.assertEqual("missing cancellation record", self.maintenance().clean_cancelled(0, apply=True)[0]["reason"])
        self.assertTrue(path.exists())

    def test_locks_exclude_data_or_file_aliases_and_release_after_error(self):
        with storage_access(self.data, self.file_root):
            with self.assertRaisesRegex(RuntimeError, "in use"):
                with storage_access(self.data, self.root / "different-files"):
                    pass
            with self.assertRaisesRegex(RuntimeError, "in use"):
                with storage_access(self.root / "different-data", self.file_root):
                    pass
        with storage_access(self.root / "different-data", self.file_root):
            pass
        self.assertTrue((self.data / ".miniim-storage.lock").exists())


if __name__ == "__main__":
    unittest.main()
