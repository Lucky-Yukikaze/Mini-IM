"""Group read accounting must use recipients recorded when each message was sent."""
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from protocol.pb import common_pb2, conversation_pb2, message_pb2, sync_pb2
from services.conversation.service import ConversationService
from services.message.service import MessageService
from storage.repo import ConversationRepo, DeliveryRepo, MessageRepo
from storage.sqlite.db import MiniImSqliteDb
from storage.sqlite.init_db import init_db


class MembershipReadTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.db"
        init_db(self.path)
        self.open()
        self.addCleanup(lambda: self.db.close())
        for user in ("alice", "bob", "carol", "dave"):
            self.conversations.ensure_user(user)
        result = self.conversations.handle_create_conversation("alice", "create",
            conversation_pb2.CreateConversation(client_conv_id="group", type=common_pb2.CONVERSATION_GROUP,
                member_ids=["bob", "carol"], title="read history"))
        self.assertTrue(result.ack.success)
        self.group = result.ack.entity_id

    def open(self):
        self.db = MiniImSqliteDb(self.path)
        self.repo = ConversationRepo(self.db)
        self.conversations = ConversationService(self.repo)
        self.messages = MessageService(MessageRepo(self.db), self.repo)
        self.delivery = DeliveryRepo(self.db)

    def send(self, intent, sender="alice"):
        result = self.messages.handle_send_message(sender, "send-" + intent,
            message_pb2.SendMessage(conversation_id=self.group, client_msg_id=intent,
                type=common_pb2.MSG_TEXT, content=b"history"))
        self.assertTrue(result.ack.success)
        return result.ack.entity_id

    def counts(self, message):
        return tuple(self.db.execute_fetchone(
            "SELECT member_count,read_count,unread_count FROM message_read_counters WHERE server_msg_id=?", (message,)))

    def read(self, user, seq):
        result = self.delivery.apply_receipt(user, self.group, seq)
        self.assertIsNotNone(result)
        return result

    def cursor(self, user):
        row = self.db.execute_fetchone(
            "SELECT last_read_seq FROM conversation_members WHERE conversation_id=? AND user_id=?", (self.group, user))
        return row[0] if row else None

    def test_new_member_reading_history_does_not_consume_original_recipients_unread(self):
        message = self.send("before-join")
        self.assertIsNotNone(self.repo.add_members("alice", self.group, ["dave"]))
        self.read("dave", 1)
        self.assertEqual((2, 0, 2), self.counts(message))
        self.assertIsNone(self.db.execute_fetchone(
            "SELECT 1 FROM message_deliveries WHERE user_id='dave' AND server_msg_id=?", (message,)))
        self.read("bob", 1)
        self.assertEqual((2, 1, 1), self.counts(message))

    def test_leave_and_rejoin_preserves_read_cursor_and_never_counts_reader_twice(self):
        message = self.send("before-leave")
        self.read("bob", 1)
        self.assertIsNotNone(self.repo.leave_conversation("bob", self.group))
        self.assertIsNone(self.delivery.apply_receipt("bob", self.group, 1))
        self.assertIsNotNone(self.repo.join_conversation("bob", self.group))
        self.assertEqual(1, self.cursor("bob"))
        result = self.read("bob", 1)
        self.assertFalse(result.updated)
        self.assertEqual([], result.sync_events)
        self.assertEqual((2, 1, 1), self.counts(message))

    def test_removed_member_added_back_reads_only_its_actual_deliveries(self):
        first = self.send("before-remove")
        self.read("bob", 1)
        self.assertIsNotNone(self.repo.remove_members("alice", self.group, ["bob"]))
        absent = self.send("while-absent")
        self.db.close()
        self.open()
        self.assertIsNotNone(self.repo.add_members("alice", self.group, ["bob"]))
        self.assertEqual(1, self.cursor("bob"))
        latest = self.send("after-return")
        self.read("bob", 3)
        self.assertEqual((2, 1, 1), self.counts(first))
        self.assertEqual((1, 0, 1), self.counts(absent))
        self.assertEqual((2, 1, 1), self.counts(latest))

    def test_repeated_membership_cycles_and_backwards_receipts_do_not_regress(self):
        first = self.send("first")
        self.read("bob", 1)
        for _ in range(3):
            self.repo.leave_conversation("bob", self.group)
            self.repo.join_conversation("bob", self.group)
            self.assertFalse(self.read("bob", 0).updated)
            self.assertFalse(self.read("bob", 1).updated)
        self.assertEqual((2, 1, 1), self.counts(first))
        second = self.send("second")
        self.read("bob", 2)
        self.read("carol", 2)
        self.assertEqual((2, 2, 0), self.counts(first))
        self.assertEqual((2, 2, 0), self.counts(second))


    def test_departure_history_and_membership_events_rollback_together(self):
        message = self.send("rollback-leave")
        self.read("bob", 1)
        for table in ("conversation_read_history", "sync_events"):
            with self.subTest(table=table):
                self.db.execute_write(f"CREATE TRIGGER reject_leave BEFORE INSERT ON {table} "
                                      "BEGIN SELECT RAISE(ABORT,'injected departure failure'); END")
                count = self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0]
                with self.assertRaises(sqlite3.Error):
                    self.repo.leave_conversation("bob", self.group)
                self.assertEqual(1, self.cursor("bob"))
                self.assertIsNone(self.db.execute_fetchone(
                    "SELECT 1 FROM conversation_read_history WHERE conversation_id=? AND user_id='bob'", (self.group,)))
                self.assertEqual(count, self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0])
                self.db.execute_write("DROP TRIGGER reject_leave")
        self.assertEqual((2, 1, 1), self.counts(message))

    def test_failed_rejoin_keeps_member_absent_and_history_available(self):
        self.send("failed-rejoin")
        self.read("bob", 1)
        self.repo.leave_conversation("bob", self.group)
        self.db.execute_write("CREATE TRIGGER reject_join BEFORE INSERT ON sync_events "
                              "BEGIN SELECT RAISE(ABORT,'injected rejoin failure'); END")
        with self.assertRaises(sqlite3.Error):
            self.repo.join_conversation("bob", self.group)
        self.assertFalse(self.repo.is_member(self.group, "bob"))
        self.assertIsNone(self.delivery.apply_receipt("bob", self.group, 1))
        self.assertEqual(1, self.db.execute_fetchone(
            "SELECT last_read_seq FROM conversation_read_history WHERE user_id='bob'")[0])
        self.db.execute_write("DROP TRIGGER reject_join")
        self.repo.join_conversation("bob", self.group)
        self.assertEqual(1, self.cursor("bob"))

    def test_read_count_rebuild_failure_rolls_back_cursor_delivery_and_events(self):
        message = self.send("rollback-read")
        count = self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0]
        self.db.execute_write("CREATE TRIGGER reject_counts BEFORE UPDATE ON message_read_counters "
                              "BEGIN SELECT RAISE(ABORT,'injected counter failure'); END")
        with self.assertRaises(sqlite3.Error):
            self.read("bob", 1)
        self.assertEqual(0, self.cursor("bob"))
        self.assertEqual((2, 0, 2), self.counts(message))
        self.assertIsNone(self.db.execute_fetchone(
            "SELECT read_at_ms FROM message_deliveries WHERE server_msg_id=? AND user_id='bob'", (message,))[0])
        self.assertEqual(count, self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0])
        self.db.execute_write("DROP TRIGGER reject_counts")
        self.read("bob", 1)
        self.assertEqual((2, 1, 1), self.counts(message))

    def test_count_events_reach_original_recipients_including_departed_sender(self):
        message = self.send("original-recipients")
        self.repo.add_members("alice", self.group, ["dave"])
        newcomer = self.read("dave", 1)
        self.assertFalse(any(event.event_type == "read_count_updated" for event in newcomer.sync_events))
        self.repo.leave_conversation("alice", self.group)
        changed = self.read("bob", 1)
        counts = [event for event in changed.sync_events if event.event_type == "read_count_updated"]
        self.assertEqual({"alice", "bob", "carol"}, {event.user_id for event in counts})
        for event in counts:
            body = sync_pb2.ReadCountUpdated.FromString(event.payload)
            self.assertEqual(event.event_id, body.event_id)
            self.assertEqual(message, body.message_id)
            self.assertEqual(1, body.unread_count)
        self.assertEqual([], self.read("bob", 1).sync_events)

    def test_count_event_failure_rolls_back_entire_receipt(self):
        message = self.send("event-failure")
        count = self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0]
        self.db.execute_write("CREATE TRIGGER reject_count_event BEFORE INSERT ON sync_events "
            "WHEN NEW.event_type='read_count_updated' BEGIN SELECT RAISE(ABORT,'injected count event failure'); END")
        with self.assertRaises(sqlite3.Error):
            self.read("bob", 1)
        self.assertEqual(0, self.cursor("bob"))
        self.assertEqual((2, 0, 2), self.counts(message))
        self.assertEqual(count, self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0])
        self.assertIsNone(self.db.execute_fetchone("SELECT read_at_ms FROM message_deliveries "
            "WHERE server_msg_id=? AND user_id='bob'", (message,))[0])
        self.db.execute_write("DROP TRIGGER reject_count_event")
        self.assertEqual(3, sum(event.event_type == "read_count_updated" for event in self.read("bob", 1).sync_events))

    def test_count_event_migration_repairs_existing_stream_once_and_rolls_back_on_failure(self):
        message = self.send("migration-count")
        self.read("bob", 1)
        self.repo.leave_conversation("alice", self.group)
        self.repo.add_members("bob", self.group, ["dave"])
        self.db.execute_write("DELETE FROM schema_migrations WHERE name='read_count_events_v1'")
        before = self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0]
        self.db.execute_write("CREATE TRIGGER fail_count_migration BEFORE INSERT ON sync_events "
            "WHEN NEW.event_type='read_count_updated' AND NEW.user_id='bob' "
            "BEGIN SELECT RAISE(ABORT,'injected count migration failure'); END")
        self.db.close()
        with self.assertRaises(sqlite3.Error):
            init_db(self.path)
        self.open()
        self.assertEqual(before, self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0])
        self.assertIsNone(self.db.execute_fetchone("SELECT 1 FROM schema_migrations WHERE name='read_count_events_v1'"))
        self.db.execute_write("DROP TRIGGER fail_count_migration")
        self.db.close()
        init_db(self.path)
        self.open()
        self.assertEqual(before + 3, self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0])
        for user in ("alice", "bob", "carol"):
            row = self.db.execute_fetchone("SELECT payload FROM sync_events WHERE user_id=? "
                "AND event_type='read_count_updated' ORDER BY seq DESC LIMIT 1", (user,))
            body = sync_pb2.ReadCountUpdated.FromString(row[0])
            self.assertEqual((message, 1), (body.message_id, body.unread_count))
        self.assertIsNone(self.db.execute_fetchone("SELECT 1 FROM sync_events "
            "WHERE user_id='dave' AND event_type='read_count_updated'"))
        self.assertIsNone(self.cursor("alice"))
        self.db.close()
        init_db(self.path)
        self.open()
        self.assertEqual(before + 3, self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0])

    def prepare_legacy(self, active):
        message = self.send("legacy")
        self.read("bob", 1)
        self.repo.leave_conversation("bob", self.group)
        if active:
            self.repo.join_conversation("bob", self.group)
            self.db.execute_write("UPDATE conversation_members SET last_read_seq=0 WHERE user_id='bob'")
        self.db.execute_write("DROP TABLE conversation_read_history")
        self.db.execute_write("DELETE FROM schema_migrations WHERE name='membership_read_history_v1'")
        self.db.execute_write("UPDATE message_read_counters SET read_count=2,unread_count=0")
        self.db.close()
        return message

    def test_old_database_restores_departed_reader_from_own_events_and_repairs_counts(self):
        message = self.prepare_legacy(active=False)
        init_db(self.path)
        self.open()
        self.assertEqual((2, 1, 1), self.counts(message))
        self.assertFalse(self.repo.is_member(self.group, "bob"))
        self.assertIsNone(self.delivery.apply_receipt("bob", self.group, 1))
        self.repo.join_conversation("bob", self.group)
        self.assertEqual(1, self.cursor("bob"))
        self.assertEqual(0, self.cursor("carol"))
        before = [tuple(row) for row in self.db.execute_fetchall("SELECT * FROM message_read_counters")]
        count = self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0]
        self.db.close()
        init_db(self.path)
        self.open()
        self.assertEqual(before, [tuple(row) for row in self.db.execute_fetchall("SELECT * FROM message_read_counters")])
        self.assertEqual(count, self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0])
        self.assertEqual("ok", self.db.execute_fetchone("PRAGMA integrity_check")[0])
        self.assertEqual([], self.db.execute_fetchall("PRAGMA foreign_key_check"))

    def test_old_database_repairs_active_member_reset_without_repeating_read(self):
        message = self.prepare_legacy(active=True)
        init_db(self.path)
        self.open()
        self.assertEqual(1, self.cursor("bob"))
        self.assertFalse(self.read("bob", 1).updated)
        self.assertEqual((2, 1, 1), self.counts(message))

    def test_migration_failure_rolls_back_history_counter_and_marker(self):
        message = self.prepare_legacy(active=True)
        self.open()
        self.db.execute_write("CREATE TRIGGER reject_migration BEFORE UPDATE ON message_read_counters "
                              "BEGIN SELECT RAISE(ABORT,'injected migration failure'); END")
        self.db.close()
        with self.assertRaises(sqlite3.Error):
            init_db(self.path)
        self.open()
        self.assertEqual(0, self.cursor("bob"))
        self.assertEqual((2, 2, 0), self.counts(message))
        self.assertEqual(0, self.db.execute_fetchone("SELECT COUNT(*) FROM conversation_read_history")[0])
        self.assertIsNone(self.db.execute_fetchone("SELECT 1 FROM schema_migrations WHERE name='membership_read_history_v1'"))
        self.db.execute_write("DROP TRIGGER reject_migration")
        self.db.close()
        init_db(self.path)
        self.open()
        self.assertEqual(1, self.cursor("bob"))
        self.assertEqual((2, 1, 1), self.counts(message))

    def test_rejoining_does_not_restart_read_or_burn_time(self):
        message = self.send("burn")
        self.db.execute_write("UPDATE messages SET burn_mode=1,burn_ttl_sec=5 WHERE server_msg_id=?", (message,))
        self.read("bob", 1)
        def times():
            return tuple(self.db.execute_fetchone(
                "SELECT read_at_ms,burn_started_at_ms,burn_at_ms FROM message_deliveries "
                "WHERE server_msg_id=? AND user_id='bob'", (message,)))
        original = times()
        self.assertTrue(all(value is not None for value in original))
        self.repo.leave_conversation("bob", self.group)
        self.repo.join_conversation("bob", self.group)
        self.read("bob", 1)
        self.assertEqual(original, times())


if __name__ == "__main__":
    unittest.main()
