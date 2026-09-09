"""Verify durable delivery confirmations independently of transport scheduling."""
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from protocol.pb import common_pb2, conversation_pb2, message_pb2, sync_pb2
from quic.server import MiniImQuicProtocol
from services.conversation.service import ConversationService
from services.message.service import MessageService
from services.sync.service import SyncService
from storage.repo import ConversationRepo, DeliveryRepo, MessageRepo, SyncRepo
from storage.sqlite.db import MiniImSqliteDb
from storage.sqlite.init_db import init_db


class DeliveryConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.db"
        init_db(self.path)
        self.open()
        self.addCleanup(lambda: self.db.close())
        conversation = ConversationService(ConversationRepo(self.db)).handle_create_conversation(
            "alice", "create", conversation_pb2.CreateConversation(client_conv_id="group",
                type=common_pb2.CONVERSATION_GROUP, member_ids=["bob", "carol"], title="test"))
        self.assertTrue(conversation.ack.success)
        self.conversation = conversation.ack.entity_id

    def open(self):
        self.db = MiniImSqliteDb(self.path)
        self.sync = SyncService(SyncRepo(self.db))

    def send(self, intent="intent"):
        sent = MessageService(MessageRepo(self.db), ConversationRepo(self.db)).handle_send_message(
            "alice", "send-" + intent, message_pb2.SendMessage(conversation_id=self.conversation,
                client_msg_id=intent, type=common_pb2.MSG_TEXT, content=b"message"))
        self.assertTrue(sent.ack.success)
        position = next(item.global_seq for item in sent.sync_events if item.user_id == "bob")
        return sent.ack.entity_id, position

    def delivery(self, message, user="bob"):
        return dict(self.db.execute_fetchone(
            "SELECT * FROM message_deliveries WHERE server_msg_id=? AND user_id=?", (message, user)))

    def confirm(self, cursor, request="confirm", device="bob-one", user="bob"):
        return self.sync.handle_sync_applied(user, device, request, sync_pb2.SyncApplied(global_cursor=cursor))

    def test_send_and_fetch_do_not_claim_received(self):
        message, cursor = self.send()
        before = self.delivery(message)
        self.assertEqual("sent", before["status"])
        self.assertGreater(before["sent_at_ms"], 0)
        self.assertIsNone(before["delivered_at_ms"])
        self.assertIsNone(before["read_at_ms"])
        self.sync.handle_sync_request("bob", sync_pb2.SyncRequest(global_cursor=cursor, limit=200))
        self.sync.handle_sync_request("bob", sync_pb2.SyncRequest(global_cursor=0, limit=200))
        self.assertEqual(before, self.delivery(message))
        sender = self.delivery(message, "alice")
        self.assertEqual("read", sender["status"])
        self.assertEqual(sender["sent_at_ms"], sender["read_at_ms"])

    def test_prefix_updates_only_received_messages_and_its_user(self):
        first, first_cursor = self.send("first")
        second, second_cursor = self.send("second")
        result = self.confirm(first_cursor)
        self.assertTrue(result.ack.success)
        self.assertEqual("delivered", self.delivery(first)["status"])
        self.assertEqual("sent", self.delivery(first, "carol")["status"])
        self.assertEqual("sent", self.delivery(second)["status"])
        self.assertEqual({"alice", "bob"}, {event.user_id for event in result.sync_events})
        self.assertEqual(2, len(result.sync_events))
        self.assertTrue(self.confirm(second_cursor, request="next").ack.success)
        self.assertEqual("delivered", self.delivery(second)["status"])
        self.assertIsNone(self.delivery(first)["read_at_ms"])
        counter = self.db.execute_fetchone(
            "SELECT read_count,unread_count FROM message_read_counters WHERE server_msg_id=?", (first,))
        self.assertEqual((0, 2), tuple(counter))

    def test_another_user_confirmation_cannot_deliver_to_recipient(self):
        message, cursor = self.send()
        before = self.delivery(message)
        result = self.confirm(cursor, user="alice", device="alice-one")
        self.assertTrue(result.ack.success)
        self.assertEqual([], result.sync_events)
        self.assertEqual(before, self.delivery(message))

    def test_confirming_delivery_events_does_not_create_an_acknowledgment_loop(self):
        zero = self.confirm(0, request="empty-prefix")
        self.assertTrue(zero.ack.success)
        self.assertEqual([], zero.sync_events)
        message, cursor = self.send()
        self.confirm(cursor)
        delivered = self.delivery(message)
        maximum = self.db.execute_fetchone("SELECT MAX(seq) FROM sync_events WHERE user_id='bob'")[0]
        self.assertGreater(maximum, cursor)
        self.assertEqual([], self.confirm(maximum, request="delivery-events").sync_events)
        self.assertEqual(delivered, self.delivery(message))
        self.assertEqual(maximum, self.db.execute_fetchone("SELECT MAX(seq) FROM sync_events WHERE user_id='bob'")[0])

    def test_replay_survives_restart_and_rejects_changed_cursor_or_device(self):
        message, cursor = self.send()
        original = self.confirm(cursor)
        delivered = self.delivery(message)
        self.db.close()
        self.open()
        replay = self.confirm(cursor)
        self.assertEqual(original.ack.SerializeToString(), replay.ack.SerializeToString())
        self.assertEqual([], replay.sync_events)
        self.assertEqual(delivered, self.delivery(message))
        self.assertEqual(409, self.confirm(cursor - 1).ack.code)
        self.assertEqual(409, self.confirm(cursor, device="bob-two").ack.code)

    def test_second_device_and_stale_cursor_never_repeat_delivery(self):
        message, cursor = self.send()
        self.confirm(cursor)
        delivered = self.delivery(message)
        self.assertEqual([], self.confirm(cursor, request="other-device", device="bob-two").sync_events)
        self.assertEqual([], self.confirm(cursor - 1, request="stale").sync_events)
        self.assertEqual(delivered, self.delivery(message))
        rows = self.db.execute_fetchall("SELECT device_id,global_cursor FROM sync_applied_cursors ORDER BY device_id")
        self.assertEqual([("bob-one", cursor), ("bob-two", cursor)], [tuple(row) for row in rows])

    def test_invalid_cursor_and_missing_device_change_no_delivery(self):
        message, cursor = self.send()
        before = self.delivery(message)
        self.assertEqual(400, self.confirm(cursor + 1).ack.code)
        self.assertEqual(400, self.confirm(cursor, request="empty-device", device="").ack.code)
        self.assertEqual(400, self.confirm(2**64 - 1, request="overflow").ack.code)
        self.assertEqual(before, self.delivery(message))
        self.assertEqual(0, self.db.execute_fetchone("SELECT COUNT(*) FROM sync_applied_cursors")[0])

    def test_cursor_events_delivery_and_ack_share_one_transaction(self):
        for table in ("sync_events", "sync_applied_cursors", "control_write_results"):
            with self.subTest(table=table):
                message, cursor = self.send(table)
                before = self.delivery(message)
                event_count = self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0]
                cursors = [tuple(row) for row in self.db.execute_fetchall("SELECT * FROM sync_applied_cursors")]
                self.db.execute_write(f"CREATE TRIGGER reject_confirm BEFORE INSERT ON {table} "
                                      "BEGIN SELECT RAISE(ABORT,'injected confirmation failure'); END")
                with self.assertRaises(sqlite3.Error):
                    self.confirm(cursor, request=table)
                self.assertEqual(before, self.delivery(message))
                self.assertEqual(event_count, self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0])
                self.assertEqual(cursors, [tuple(row) for row in self.db.execute_fetchall("SELECT * FROM sync_applied_cursors")])
                self.assertIsNone(self.db.execute_fetchone(
                    "SELECT ack FROM control_write_results WHERE request_id=?", (table,)))
                self.db.execute_write("DROP TRIGGER reject_confirm")
                self.assertTrue(self.confirm(cursor, request=table).ack.success)

    def test_read_is_receiving_evidence_and_late_confirmation_cannot_regress(self):
        message, cursor = self.send()
        DeliveryRepo(self.db).apply_receipt("bob", self.conversation, 1)
        read = self.delivery(message)
        self.assertEqual("read", read["status"])
        self.assertIsNotNone(read["delivered_at_ms"])
        self.assertEqual(read["read_at_ms"], read["delivered_at_ms"])
        self.assertEqual([], self.confirm(cursor).sync_events)
        self.assertEqual(read, self.delivery(message))

    def test_confirmation_does_not_restore_failed_delivery_or_start_burn_timer(self):
        message, cursor = self.send()
        self.db.execute_write("UPDATE messages SET burn_mode=1,burn_ttl_sec=5 WHERE server_msg_id=?", (message,))
        self.confirm(cursor)
        self.assertIsNone(self.delivery(message)["burn_started_at_ms"])
        second, position = self.send("failed")
        self.db.execute_write("UPDATE message_deliveries SET status='failed',failed_at_ms=7,failure_reason='denied' "
                              "WHERE server_msg_id=? AND user_id='bob'", (second,))
        failed = self.delivery(second)
        self.assertEqual([], self.confirm(position, request="after-failure").sync_events)
        self.assertEqual(failed, self.delivery(second))

    def test_live_and_replayed_delivery_events_have_identical_identity_and_payload(self):
        message, cursor = self.send()
        result = self.confirm(cursor)
        packets = []
        protocol = SimpleNamespace(m_control_stream_id=0, m_session_id="session", m_device_id="device",
                                   _send=lambda stream, envelope: packets.append(envelope))
        for event in result.sync_events:
            MiniImQuicProtocol.send_sync_event(protocol, event)
            live = packets[-1].sync_response.events[0]
            replay, _ = self.sync.handle_sync_request(event.user_id,
                sync_pb2.SyncRequest(global_cursor=event.global_seq - 1, limit=1))
            self.assertEqual(live.SerializeToString(), replay.events[0].SerializeToString())
            self.assertEqual(event.payload, live.delivery_updated.SerializeToString())
            self.assertEqual(message, live.delivery_updated.message_id)
            self.assertEqual(self.delivery(message)["delivered_at_ms"], live.delivery_updated.delivered_at_ms)

    def make_legacy(self):
        message, cursor = self.send()
        DeliveryRepo(self.db).apply_receipt("carol", self.conversation, 1)
        self.db.execute_write("ALTER TABLE message_deliveries DROP COLUMN sent_at_ms")
        self.db.execute_write("UPDATE message_deliveries SET status='delivered',delivered_at_ms=1 WHERE user_id='bob'")
        self.db.close()
        return message, cursor

    def test_legacy_upgrade_removes_unproven_delivery_and_runs_once(self):
        message, cursor = self.make_legacy()
        init_db(self.path)
        self.open()
        self.assertEqual("sent", self.delivery(message)["status"])
        self.assertIsNone(self.delivery(message)["delivered_at_ms"])
        self.assertGreater(self.delivery(message)["sent_at_ms"], 0)
        carol = self.delivery(message, "carol")
        self.assertEqual("read", carol["status"])
        self.assertEqual(carol["read_at_ms"], carol["delivered_at_ms"])
        self.confirm(cursor)
        delivered = self.delivery(message)
        self.db.close()
        init_db(self.path)
        self.open()
        self.assertEqual(delivered, self.delivery(message))
        self.assertEqual("ok", self.db.execute_fetchone("PRAGMA integrity_check")[0])
        self.assertEqual([], self.db.execute_fetchall("PRAGMA foreign_key_check"))

    def test_legacy_upgrade_failure_does_not_leave_migration_marker(self):
        message, _ = self.make_legacy()
        self.open()
        self.db.execute_write("CREATE TRIGGER reject_migration BEFORE UPDATE ON message_deliveries "
                              "BEGIN SELECT RAISE(ABORT,'injected migration failure'); END")
        self.db.close()
        with self.assertRaises(sqlite3.Error):
            init_db(self.path)
        self.open()
        columns = {row["name"] for row in self.db.execute_fetchall("PRAGMA table_info(message_deliveries)")}
        self.assertNotIn("sent_at_ms", columns)
        self.assertEqual("delivered", self.delivery(message)["status"])
        self.db.execute_write("DROP TRIGGER reject_migration")
        self.db.close()
        init_db(self.path)
        self.open()
        self.assertEqual("sent", self.delivery(message)["status"])


if __name__ == "__main__":
    unittest.main()
