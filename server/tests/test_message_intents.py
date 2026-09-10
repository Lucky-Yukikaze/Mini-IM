"""Message intent conflicts, durable identity and legacy redaction boundaries."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from protocol.pb import common_pb2, conversation_pb2, message_pb2, envelope_pb2
from services.control.service import ControlWriteService
from services.delivery.service import DeliveryService
from storage.repo.control_write_repo import ControlWriteRepo
from services.conversation.service import ConversationService
from services.message.service import MessageService
from storage.repo import ConversationRepo, MessageRepo, DeliveryRepo
from storage.sqlite.db import MiniImSqliteDb
from storage.sqlite.init_db import init_db


class MessageIntentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.db"
        self.open()
        result = ConversationService(ConversationRepo(self.db)).handle_create_conversation(
            "alice", "group", conversation_pb2.CreateConversation(client_conv_id="group",
                type=common_pb2.CONVERSATION_GROUP, title="test", member_ids=["bob"]))
        self.conversation = result.ack.entity_id
        self.request = message_pb2.SendMessage(conversation_id=self.conversation, client_msg_id="intent",
            type=common_pb2.MSG_TEXT, content=b"original private text", burn_mode=1, burn_ttl_sec=5)

    def open(self):
        init_db(self.path)
        self.db = MiniImSqliteDb(self.path)
        self.service = MessageService(MessageRepo(self.db), ConversationRepo(self.db))

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def send(self, request=None, request_id="first"):
        return self.service.handle_send_message("alice", request_id, request or self.request)

    def counts(self):
        return tuple(self.db.execute_fetchone("SELECT COUNT(*) FROM " + table)[0]
                     for table in ("messages", "message_deliveries", "sync_events"))

    def rename(self, request_id, title="renamed"):
        service = ControlWriteService(ControlWriteRepo(self.db), ConversationService(ConversationRepo(self.db)),
                                      DeliveryService(DeliveryRepo(self.db)))
        envelope = envelope_pb2.Envelope(request_id=request_id)
        envelope.rename_conversation.conversation_id = self.conversation
        envelope.rename_conversation.title = title
        return service.handle("alice", envelope)

    def test_request_id_cannot_change_message_intent_or_operation(self):
        original = self.send()
        before = self.counts()
        changed = message_pb2.SendMessage(); changed.CopyFrom(self.request); changed.client_msg_id = "other-intent"
        self.assertEqual(409, self.send(changed).ack.code)
        self.assertEqual(409, self.rename("first").ack.code)
        self.assertEqual(before, self.counts())
        self.db.close(); self.open()
        self.assertEqual(original.ack.SerializeToString(), self.send().ack.SerializeToString())
        self.assertEqual(409, self.rename("first").ack.code)
        self.assertTrue(self.rename("control-first").ack.success)
        self.assertEqual(409, self.send(request_id="control-first").ack.code)

    def test_definitive_rejection_is_replayed_without_accepting_changed_body(self):
        invalid = message_pb2.SendMessage(); invalid.CopyFrom(self.request); invalid.burn_ttl_sec = 1
        rejection = self.send(invalid)
        self.assertEqual(400, rejection.ack.code)
        self.db.close(); self.open()
        self.assertEqual(rejection.ack.SerializeToString(), self.send(invalid).ack.SerializeToString())
        self.assertEqual(409, self.send().ack.code)
        self.assertTrue(self.send(request_id="corrected-new-request").ack.success)

    def test_legacy_message_request_reserves_id_before_first_replay(self):
        original = self.send()
        self.db.execute_write("DELETE FROM control_write_results")
        self.db.close(); self.open()
        self.assertEqual(409, self.rename("first").ack.code)
        changed = message_pb2.SendMessage(); changed.CopyFrom(self.request); changed.client_msg_id = "new-intent"
        self.assertEqual(409, self.send(changed).ack.code)
        self.assertEqual(original.ack.entity_id, self.send().ack.entity_id)
        self.assertEqual(1, self.counts()[0])

    def test_legacy_duplicate_request_ids_are_not_silently_assigned(self):
        self.assertTrue(self.send().ack.success)
        self.db.execute_write("DELETE FROM control_write_results")
        changed = message_pb2.SendMessage(); changed.CopyFrom(self.request); changed.client_msg_id = "legacy-collision"
        MessageRepo(self.db).append_message("first", "alice", changed, ["alice", "bob"])
        self.db.close(); self.open()
        before = self.counts()
        self.assertEqual(409, self.send().ack.code)
        self.assertEqual(409, self.rename("first").ack.code)
        self.assertEqual(before, self.counts())

    def test_request_result_failure_rolls_back_message_and_events(self):
        self.db.execute_write("CREATE TRIGGER fail_result BEFORE INSERT ON control_write_results BEGIN SELECT RAISE(ABORT,'result failure'); END")
        before = self.counts()
        with self.assertRaises(Exception):
            self.send()
        self.assertEqual(before, self.counts())
        self.db.execute_write("DROP TRIGGER fail_result")
        self.assertTrue(self.send().ack.success)

    def test_changed_content_type_or_burn_settings_reject_same_intent(self):
        self.assertTrue(self.send().ack.success)
        before = self.counts()
        for field, value in (("content", b"different"), ("type", common_pb2.MSG_FILE),
                             ("burn_mode", 0), ("burn_ttl_sec", 6)):
            with self.subTest(field=field):
                changed = message_pb2.SendMessage(); changed.CopyFrom(self.request)
                setattr(changed, field, value)
                result = self.send(changed, "new-device-request")
                self.assertFalse(result.ack.success)
                self.assertEqual(409, result.ack.code)
                self.assertIsNone(result.message_push)
                self.assertEqual([], result.sync_events)
                self.assertEqual(before, self.counts())

    def test_original_intent_survives_redaction_restart_and_policy_change(self):
        result = self.send()
        self.db.execute_write("UPDATE message_deliveries SET burn_at_ms=1")
        delivery = DeliveryRepo(self.db)
        delivery.collect_due_burn_sync_events(100)
        delivery.purge_burned_message_content(100)
        self.assertEqual(b"", self.db.execute_fetchone("SELECT content FROM messages")[0])
        before = self.counts()
        self.db.close(); self.open()
        self.service = MessageService(MessageRepo(self.db), ConversationRepo(self.db), burn_enabled=False)
        replay = self.send(request_id="after-restart")
        self.assertTrue(replay.ack.success)
        self.assertEqual(result.ack.entity_id, replay.ack.entity_id)
        changed = message_pb2.SendMessage(); changed.CopyFrom(self.request); changed.content = b"changed"
        self.assertEqual(409, self.send(changed).ack.code)
        self.assertEqual(before, self.counts())
        self.assertEqual(b"", self.db.execute_fetchone("SELECT content FROM messages")[0])

    def test_defaults_have_one_identity(self):
        self.request.type = common_pb2.MSG_UNSPECIFIED
        self.request.burn_mode = 0
        self.request.burn_ttl_sec = 99
        original = self.send()
        self.request.type = common_pb2.MSG_TEXT
        self.request.burn_ttl_sec = 0
        self.assertEqual(original.ack.entity_id, self.send(request_id="retry").ack.entity_id)
        self.assertEqual(1, self.counts()[0])

    def test_disabled_burn_keeps_original_requested_identity(self):
        self.service = MessageService(MessageRepo(self.db), ConversationRepo(self.db), burn_enabled=False)
        self.assertTrue(self.send().ack.success)
        self.db.close(); self.open()
        self.assertTrue(self.send(request_id="enabled-now").ack.success)
        changed = message_pb2.SendMessage(); changed.CopyFrom(self.request); changed.burn_ttl_sec = 6
        self.assertEqual(409, self.send(changed).ack.code)
        self.assertEqual(0, self.db.execute_fetchone("SELECT burn_mode FROM messages")[0])

    def test_legacy_intact_rows_migrate_without_resetting_events(self):
        self.assertTrue(self.send().ack.success)
        before = self.counts()
        self.db.execute_write("DELETE FROM control_write_results")
        self.db.execute_write("ALTER TABLE messages DROP COLUMN intent_fingerprint")
        self.db.close(); self.open()
        self.assertTrue(self.send().ack.success)
        self.request.content = b"conflict"
        self.assertEqual(409, self.send().ack.code)
        self.assertEqual(before, self.counts())
        self.db.close(); self.open()
        self.assertEqual(before, self.counts())

    def test_legacy_purged_rows_cannot_claim_content_matches(self):
        self.assertTrue(self.send().ack.success)
        self.db.execute_write("UPDATE message_deliveries SET burn_at_ms=1")
        delivery = DeliveryRepo(self.db); delivery.collect_due_burn_sync_events(100)
        delivery.purge_burned_message_content(100)
        self.db.execute_write("DELETE FROM control_write_results")
        self.db.execute_write("ALTER TABLE messages DROP COLUMN intent_fingerprint")
        self.db.close(); self.open()
        before = self.counts()
        result = self.send()
        self.assertEqual(409, result.ack.code)
        self.assertIn("unavailable", result.ack.message)
        self.assertEqual(before, self.counts())
        self.assertEqual(b"", self.db.execute_fetchone("SELECT content FROM messages")[0])

    def test_legacy_backfill_failure_rolls_back_column_and_data(self):
        self.assertTrue(self.send().ack.success)
        before = self.counts()
        self.db.execute_write("DELETE FROM control_write_results")
        self.db.execute_write("ALTER TABLE messages DROP COLUMN intent_fingerprint")
        self.db.execute_write("CREATE TRIGGER reject_migration BEFORE UPDATE ON messages BEGIN SELECT RAISE(ABORT,'migration failure'); END")
        self.db.close()
        with self.assertRaises(Exception):
            init_db(self.path)
        self.db = MiniImSqliteDb(self.path)
        self.assertNotIn("intent_fingerprint", {row["name"] for row in self.db.execute_fetchall("PRAGMA table_info(messages)")})
        self.assertEqual(before, self.counts())
        self.db.execute_write("DROP TRIGGER reject_migration")
        self.db.close(); self.open()
        self.assertTrue(self.send().ack.success)

    def test_fingerprint_and_message_rollback_together(self):
        self.db.execute_write("CREATE TRIGGER fail_event BEFORE INSERT ON sync_events BEGIN SELECT RAISE(ABORT,'injected'); END")
        before = self.counts()
        with self.assertRaises(Exception):
            self.send()
        self.assertEqual(before, self.counts())
        self.db.execute_write("DROP TRIGGER fail_event")
        self.assertTrue(self.send().ack.success)
        self.assertEqual(32, len(self.db.execute_fetchone("SELECT intent_fingerprint FROM messages")[0]))
