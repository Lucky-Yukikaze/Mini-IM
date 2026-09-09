import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quic.server import MiniImQuicProtocol
from protocol.pb import common_pb2, conversation_pb2, file_pb2, message_pb2, sync_pb2
from services.conversation.service import ConversationService
from services.file.service import FileService
from services.message.service import MessageService
from services.sync.service import SyncService
from storage.repo import ConversationRepo, DeliveryRepo, FileRepo, MessageRepo, SyncRepo
from storage.sqlite.db import MiniImSqliteDb
from storage.sqlite.init_db import init_db


class ReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.m_temp = tempfile.TemporaryDirectory()
        self.m_db_path = Path(self.m_temp.name) / "test.db"
        self.m_file_root = Path(self.m_temp.name) / "files"
        self.m_db = MiniImSqliteDb(self.m_db_path)
        self.m_db.init_schema()
        self._build_services()
        created = self.m_conversations.handle_create_conversation(
            "u-alice", "create",
            conversation_pb2.CreateConversation(
                client_conv_id="group", type=common_pb2.CONVERSATION_GROUP,
                member_ids=["u-bob"], title="test",
            ),
        )
        self.assertTrue(created.ack.success)
        self.m_conversation_id = created.ack.entity_id
        self.m_created_events = created.sync_events

    def tearDown(self):
        self.m_db.close()
        self.m_temp.cleanup()

    def _build_services(self):
        conversation_repo = ConversationRepo(self.m_db)
        self.m_message_repo = MessageRepo(self.m_db)
        self.m_file_repo = FileRepo(self.m_db)
        self.m_conversations = ConversationService(conversation_repo)
        self.m_messages = MessageService(self.m_message_repo, conversation_repo)
        self.m_files = FileService(
            self.m_file_repo, conversation_repo, self.m_message_repo, self.m_file_root, 1000,
        )
        self.m_delivery = DeliveryRepo(self.m_db)
        self.m_sync = SyncService(SyncRepo(self.m_db))

    def _send(self, client_id="message", content=b"private content", burn=False, message_type=common_pb2.MSG_TEXT):
        result = self.m_messages.handle_send_message(
            "u-alice", "send-" + client_id,
            message_pb2.SendMessage(
                conversation_id=self.m_conversation_id, client_msg_id=client_id,
                type=message_type, content=content, burn_mode=1 if burn else 0,
                burn_ttl_sec=5 if burn else 0,
            ),
        )
        self.assertTrue(result.ack.success)
        return result

    def _upload(self):
        payload = b"atomic completion"
        result = self.m_files.handle_file_init(
            "u-alice", "file-init",
            file_pb2.FileInit(
                conversation_id=self.m_conversation_id, client_file_id="intent",
                file_name="test.bin", file_size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(), direction=common_pb2.FILE_DIRECTION_UPLOAD,
            ),
        )
        self.assertTrue(result.ack.success)
        self.m_files.append_file_chunk("u-alice", result.ack.entity_id, payload)
        return result

    def _replayed_message(self, user_id, message_id):
        response, _ = self.m_sync.handle_sync_request(
            user_id, sync_pb2.SyncRequest(global_cursor=0, limit=200),
        )
        return next(event.message for event in response.events
                    if event.HasField("message") and event.message.message_id == message_id)

    def _expire(self, message_id, user_id=None):
        sql = "UPDATE message_deliveries SET burn_at_ms = 1 WHERE server_msg_id = ?"
        params = [message_id]
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        self.m_db.execute_write(sql, tuple(params))
        return self.m_delivery.collect_due_burn_sync_events(100)

    def test_event_identity_matches_live_payload_and_replay_for_every_kind(self):
        sent = self._send()
        receipt = self.m_delivery.apply_receipt("u-bob", self.m_conversation_id, 1)
        recalled = self.m_delivery.apply_recall("u-alice", self.m_conversation_id, sent.ack.entity_id)
        uploaded = self._upload()
        burn = self._send("burn", burn=True)
        burned = self._expire(burn.ack.entity_id)
        events = (self.m_created_events + sent.sync_events + receipt.sync_events
                  + recalled.sync_events + uploaded.sync_events + burn.sync_events + burned)
        self.assertEqual(
            {"conversation_updated", "message", "receipt", "recall", "file_updated", "read_count_updated"},
            {event.event_type for event in events},
        )
        for event in events:
            with self.subTest(kind=event.event_type, user=event.user_id):
                row = self.m_db.execute_fetchone(
                    "SELECT event_id FROM sync_events WHERE user_id = ? AND seq = ?",
                    (event.user_id, event.global_seq),
                )
                self.assertEqual(row["event_id"], event.event_id)
                replay, _ = self.m_sync.handle_sync_request(
                    event.user_id, sync_pb2.SyncRequest(global_cursor=event.global_seq - 1, limit=1),
                )
                self.assertEqual(replay.events[0].event_id, event.event_id)
                self.assertEqual(replay.events[0].global_seq, event.global_seq)
                body = getattr(replay.events[0], event.event_type)
                if "event_id" in body.DESCRIPTOR.fields_by_name:
                    self.assertEqual(event.event_id, body.event_id)
        self.assertEqual(len(events), len({event.event_id for event in events}))

    def test_file_completion_rolls_back_on_failure_then_recovers_after_reopen(self):
        uploaded = self._upload()
        file_id = uploaded.ack.entity_id
        finish = file_pb2.FileFinish(file_id=file_id, success=True)
        before_events = self.m_db.execute_fetchone("SELECT COUNT(*) AS n FROM sync_events")["n"]
        with patch.object(self.m_files, "_append_file_message", side_effect=RuntimeError("injected")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.m_files.handle_file_finish("u-alice", "finish", finish)
        self.m_db.close()
        self.m_db = MiniImSqliteDb(self.m_db_path)
        self._build_services()
        self.assertNotEqual("completed", self.m_files.get_transfer_by_file_id(file_id).status)
        self.assertEqual(before_events, self.m_db.execute_fetchone("SELECT COUNT(*) AS n FROM sync_events")["n"])
        self.assertTrue(self.m_files.handle_file_finish("u-alice", "finish", finish).ack.success)
        self.assertTrue(self.m_files.handle_file_finish("u-alice", "finish", finish).ack.success)
        count = self.m_db.execute_fetchone(
            "SELECT COUNT(*) AS n FROM messages WHERE client_msg_id = ?", ("file-msg-" + file_id,),
        )["n"]
        self.assertEqual(1, count)

    def test_retry_repairs_preexisting_completed_file_without_message(self):
        uploaded = self._upload()
        file_id = uploaded.ack.entity_id
        self.m_file_repo.apply_finish(file_id, True, ["u-alice", "u-bob"])
        result = self.m_files.handle_file_finish(
            "u-alice", "finish", file_pb2.FileFinish(file_id=file_id, success=True),
        )
        self.assertTrue(result.ack.success)
        message = self.m_message_repo.get_message_by_client_msg_id(
            self.m_conversation_id, "u-alice", "file-msg-" + file_id,
        )
        self.assertIsNotNone(message)

    def test_burn_redacts_only_expired_user_then_all_copies(self):
        sent = self._send(burn=True)
        message_id = sent.ack.entity_id
        self._expire(message_id, "u-alice")
        self.assertEqual(b"", self._replayed_message("u-alice", message_id).content)
        self.assertTrue(self._replayed_message("u-alice", message_id).recalled)
        self.assertEqual(b"private content", self._replayed_message("u-bob", message_id).content)
        self._expire(message_id, "u-bob")
        self.assertEqual(1, self.m_delivery.purge_burned_message_content(100))
        self.assertEqual(0, self.m_delivery.purge_burned_message_content(100))
        self.assertEqual(b"", self._replayed_message("u-bob", message_id).content)
        rows = self.m_db.execute_fetchall(
            "SELECT payload FROM sync_events WHERE event_type = 'message' AND entity_id = ?", (message_id,),
        )
        self.assertTrue(all(message_pb2.Message.FromString(row["payload"]).content == b"" for row in rows))
        self.assertEqual(b"", self.m_message_repo.get_message_by_id(self.m_conversation_id, message_id).content)

    def test_file_purge_is_idempotent_and_does_not_starve_following_messages(self):
        first = self._send(
            "file", json.dumps({"kind": "file", "fileId": "f1", "fileName": "private"}).encode(),
            burn=True, message_type=common_pb2.MSG_FILE,
        )
        second = self._send("text", burn=True)
        self._expire(first.ack.entity_id)
        self._expire(second.ack.entity_id)
        self.assertEqual(1, self.m_delivery.purge_burned_message_content(1))
        self.assertEqual(1, self.m_delivery.purge_burned_message_content(1))
        self.assertEqual(0, self.m_delivery.purge_burned_message_content(1))
        for user_id in ("u-alice", "u-bob"):
            payload = json.loads(self._replayed_message(user_id, first.ack.entity_id).content)
            self.assertEqual({"kind": "file", "fileId": "f1"}, payload)

    def test_migration_backfills_and_redacts_legacy_event_copies(self):
        sent = self._send(burn=True)
        message_id = sent.ack.entity_id
        self._expire(message_id)
        original = sent.message_push.messages[0].SerializeToString()
        self.m_db.execute_write("UPDATE sync_events SET payload = ? WHERE event_type = 'message'", (original,))
        self.m_db.m_connection.execute("DROP INDEX idx_sync_events_entity")
        self.m_db.m_connection.execute("ALTER TABLE sync_events DROP COLUMN entity_id")
        self.m_db.m_connection.execute("ALTER TABLE messages DROP COLUMN content_purged_at_ms")
        self.m_db.close()
        init_db(self.m_db_path)
        self.m_db = MiniImSqliteDb(self.m_db_path)
        self._build_services()
        self.assertEqual(b"", self._replayed_message("u-bob", message_id).content)
        self.assertEqual(1, self.m_db.execute_fetchone("PRAGMA foreign_keys")[0])
        self.assertEqual(2, self.m_db.execute_fetchone(
            "SELECT COUNT(*) FROM sync_events WHERE entity_id = ?", (message_id,),
        )[0])
        init_db(self.m_db_path)
        self.assertEqual(b"", self._replayed_message("u-alice", message_id).content)


    def test_download_requires_verified_receiver_confirmation(self):
        upload = self._upload()
        source_id = upload.ack.entity_id
        self.assertTrue(self.m_files.handle_file_finish(
            "u-alice", "finish", file_pb2.FileFinish(file_id=source_id, success=True),
        ).ack.success)
        download = self.m_files.handle_file_init(
            "u-bob", "download",
            file_pb2.FileInit(
                conversation_id=self.m_conversation_id, client_file_id="download-intent",
                direction=common_pb2.FILE_DIRECTION_DOWNLOAD, source_file_id=source_id,
            ),
        )
        self.assertTrue(download.ack.success)
        file_id = download.ack.entity_id
        scheduled = []
        fake = SimpleNamespace(
            m_file_service=self.m_files,
            m_download_sender=SimpleNamespace(start=lambda *args: scheduled.append(args)),
        )
        MiniImQuicProtocol._send_download_stream(fake, "u-bob", source_id, file_id, 0)
        self.assertEqual(file_id, scheduled[0][0])
        self.assertEqual(self.m_files.get_storage_path(source_id), scheduled[0][1])
        transfer = self.m_files.get_transfer_by_file_id(file_id)
        self.assertNotEqual("completed", transfer.status)
        self.assertEqual(0, transfer.received_bytes)
        for size, digest in [(0, ""), (transfer.file_size - 1, transfer.sha256),
                             (transfer.file_size, "0" * 64)]:
            with self.subTest(size=size, digest=digest):
                rejected = self.m_files.handle_file_finish(
                    "u-bob", "invalid-confirmation", file_pb2.FileFinish(
                        file_id=file_id, success=True, transferred_bytes=size, sha256=digest,
                    ),
                )
                self.assertFalse(rejected.ack.success)
                self.assertNotEqual("completed", self.m_files.get_transfer_by_file_id(file_id).status)
        for request_id in ("confirmation", "confirmation", "confirmation-retry"):
            result = self.m_files.handle_file_finish(
                "u-bob", request_id, file_pb2.FileFinish(
                    file_id=file_id, success=True, transferred_bytes=transfer.file_size, sha256=transfer.sha256,
                ),
            )
            self.assertTrue(result.ack.success)
            self.assertTrue(result.file_updated.completed)
        self.assertEqual(1, self.m_db.execute_fetchone("SELECT COUNT(*) FROM messages")[0])

    def test_file_intent_rejects_changed_metadata_and_other_conversation(self):
        uploaded = self._upload()
        original = self.m_files.get_transfer_by_file_id(uploaded.ack.entity_id)
        baseline = file_pb2.FileInit(
            conversation_id=self.m_conversation_id, client_file_id="intent", file_name=original.file_name,
            file_size=original.file_size, sha256=original.sha256, direction=original.direction,
        )
        other = self.m_conversations.handle_create_conversation(
            "u-alice", "other-conversation",
            conversation_pb2.CreateConversation(
                client_conv_id="other", type=common_pb2.CONVERSATION_GROUP, member_ids=["u-bob"], title="other",
            ),
        )
        for field, value in (("sha256", "0" * 64), ("file_name", "changed.bin"),
                             ("conversation_id", other.ack.entity_id), ("source_file_id", "unexpected")):
            with self.subTest(field=field):
                request = file_pb2.FileInit()
                request.CopyFrom(baseline)
                setattr(request, field, value)
                result = self.m_files.handle_file_init("u-alice", "retry", request)
                self.assertFalse(result.ack.success)
                self.assertIn(result.ack.code, (400, 409))
                self.assertEqual(original, self.m_files.get_transfer_by_file_id(original.file_id))

    def test_completed_file_does_not_regress_or_append_duplicate_events(self):
        uploaded = self._upload()
        file_id = uploaded.ack.entity_id
        self.m_files.handle_file_finish("u-alice", "finish", file_pb2.FileFinish(file_id=file_id, success=True))
        original = self.m_files.get_transfer_by_file_id(file_id)
        count = self.m_db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0]
        for success in (True, False, True):
            result = self.m_files.handle_file_finish(
                "u-alice", "finish-retry", file_pb2.FileFinish(file_id=file_id, success=success),
            )
            self.assertTrue(result.ack.success)
            self.assertEqual("completed", result.file_updated.status)
            self.assertEqual(original.version, result.file_updated.version)
            self.assertEqual([], result.sync_events)
        self.assertEqual(count, self.m_db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0])

    def test_download_intent_is_bound_to_source_and_direction(self):
        uploaded = self._upload()
        source = uploaded.ack.entity_id
        self.m_files.handle_file_finish("u-alice", "finish", file_pb2.FileFinish(file_id=source, success=True))
        request = file_pb2.FileInit(
            conversation_id=self.m_conversation_id, client_file_id="intent",
            direction=common_pb2.FILE_DIRECTION_DOWNLOAD, source_file_id=source,
        )
        self.assertFalse(self.m_files.handle_file_init("u-alice", "direction-change", request).ack.success)
        request.client_file_id = "download"
        first = self.m_files.handle_file_init("u-alice", "download", request)
        self.assertTrue(first.ack.success)
        # A second completed file has identical metadata but represents a different source entity.
        original = self.m_files.get_transfer_by_file_id(source)
        second = self.m_files.handle_file_init("u-alice", "second", file_pb2.FileInit(
            conversation_id=self.m_conversation_id, client_file_id="second", file_name=original.file_name,
            file_size=original.file_size, sha256=original.sha256, direction=common_pb2.FILE_DIRECTION_UPLOAD,
        ))
        self.m_files.append_file_chunk("u-alice", second.ack.entity_id, b"atomic completion")
        self.m_files.handle_file_finish(
            "u-alice", "second-finish", file_pb2.FileFinish(file_id=second.ack.entity_id, success=True),
        )
        request.source_file_id = second.ack.entity_id
        conflict = self.m_files.handle_file_init("u-alice", "changed-source", request)
        self.assertFalse(conflict.ack.success)
        self.assertEqual(409, conflict.ack.code)
        self.assertEqual(1, self.m_db.execute_fetchone(
            "SELECT COUNT(*) FROM file_transfers WHERE client_file_id = 'download'",
        )[0])

    def test_file_intents_survive_reopen_and_legacy_upgrade_preserves_rows(self):
        uploaded = self._upload()
        source_id = uploaded.ack.entity_id
        self.m_files.handle_file_finish(
            "u-alice", "finish", file_pb2.FileFinish(file_id=source_id, success=True),
        )
        request = file_pb2.FileInit(
            conversation_id=self.m_conversation_id, client_file_id="download-migrate",
            direction=common_pb2.FILE_DIRECTION_DOWNLOAD, source_file_id=source_id,
        )
        download = self.m_files.handle_file_init("u-bob", "download", request)
        self.m_db.close()
        self.m_db = MiniImSqliteDb(self.m_db_path)
        self._build_services()
        self.assertEqual(source_id, self.m_files.get_transfer_by_file_id(download.ack.entity_id).source_file_id)
        self.assertEqual(download.ack.entity_id, self.m_files.handle_file_init("u-bob", "retry", request).ack.entity_id)
        original_bytes = self.m_files.get_storage_path(source_id).read_bytes()
        self.m_db.execute_write("ALTER TABLE file_transfers DROP COLUMN source_file_id")
        self.m_db.close()
        init_db(self.m_db_path)
        init_db(self.m_db_path)
        self.m_db = MiniImSqliteDb(self.m_db_path)
        self._build_services()
        self.assertEqual(2, self.m_db.execute_fetchone("SELECT COUNT(*) FROM file_transfers")[0])
        self.assertEqual("completed", self.m_files.get_transfer_by_file_id(source_id).status)
        self.assertEqual(original_bytes, self.m_files.get_storage_path(source_id).read_bytes())
        # The old schema never stored a download source. Refuse to guess it from matching bytes.
        result = self.m_files.handle_file_init("u-bob", "legacy-retry", request)
        self.assertFalse(result.ack.success)
        self.assertEqual(409, result.ack.code)
        self.assertEqual("", self.m_files.get_transfer_by_file_id(download.ack.entity_id).source_file_id)

    def test_unchanged_file_init_and_progress_do_not_create_events(self):
        uploaded = self._upload()
        original = self.m_files.get_transfer_by_file_id(uploaded.ack.entity_id)
        event_count = self.m_db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0]
        result = self.m_files.handle_file_init("u-alice", "retry", file_pb2.FileInit(
            conversation_id=original.conversation_id, client_file_id=original.client_file_id,
            file_name=original.file_name, file_size=original.file_size, sha256=original.sha256.upper(),
            direction=original.direction, resume_offset=original.received_bytes,
        ))
        self.assertTrue(result.ack.success)
        self.assertEqual([], result.sync_events)
        progress = self.m_file_repo.apply_progress(original.file_id, original.received_bytes, ["u-alice", "u-bob"])
        self.assertFalse(progress.changed)
        self.assertEqual([], progress.sync_events)
        self.assertEqual(event_count, self.m_db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0])

    def test_upload_refuses_excess_bytes_and_completed_file_mutation(self):
        upload = self._upload()
        file_id = upload.ack.entity_id
        original = self.m_files.get_storage_path(file_id).read_bytes()
        progress, _ = self.m_files.append_file_chunk("u-alice", file_id, b"unexpected")
        self.assertIsNone(progress)
        self.assertEqual(original, self.m_files.get_storage_path(file_id).read_bytes())
        self.assertTrue(self.m_files.handle_file_finish(
            "u-alice", "finish", file_pb2.FileFinish(file_id=file_id, success=True),
        ).ack.success)
        progress, _ = self.m_files.append_file_chunk("u-alice", file_id, b"unexpected")
        self.assertIsNone(progress)
        self.assertEqual(original, self.m_files.get_storage_path(file_id).read_bytes())


if __name__ == "__main__":
    unittest.main()
