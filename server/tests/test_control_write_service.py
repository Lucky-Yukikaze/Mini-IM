"""Check persistent control-write semantics against real SQLite business repositories."""
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from protocol.pb import common_pb2, conversation_pb2, envelope_pb2, message_pb2
from services.control.service import ControlWriteService
from services.conversation.service import ConversationService
from services.delivery.service import DeliveryService
from services.message.service import MessageService
from storage.repo import ConversationRepo, DeliveryRepo, MessageRepo
from storage.repo.control_write_repo import ControlWriteRepo, ControlWriteResult
from storage.sqlite.db import MiniImSqliteDb
from storage.sqlite.init_db import init_db


class ControlWriteServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.db"
        self.db = MiniImSqliteDb(self.path)
        self.db.init_schema()
        self.build_services()
        for user in ["alice", "bob", "cindy"]:
            self.conversations.ensure_user(user)
        self.create = conversation_pb2.CreateConversation(
            client_conv_id="group", type=common_pb2.CONVERSATION_GROUP, title="original", member_ids=["bob"])
        self.created = self.call("alice", "create", create_conversation=self.create)
        self.conversation = self.created.ack.entity_id

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def build_services(self):
        self.repo = ConversationRepo(self.db)
        self.conversations = ConversationService(self.repo)
        self.messages = MessageService(MessageRepo(self.db), self.repo)
        self.requests = ControlWriteRepo(self.db)
        self.service = ControlWriteService(self.requests, self.conversations, DeliveryService(DeliveryRepo(self.db)))

    def call(self, user, request, **body):
        return self.service.handle(user, envelope_pb2.Envelope(request_id=request, **body))

    def count(self):
        return self.db.execute_fetchone("SELECT COUNT(*) FROM sync_events")[0]

    def title(self):
        return self.repo.get_conversation(self.conversation).title

    def test_all_membership_replays_preserve_later_changes(self):
        cases = [
            ("add_members", "remove_members", "alice", ["cindy"], False),
            ("remove_members", "add_members", "alice", ["bob"], True),
            ("leave_conversation", "join_conversation", "bob", [], True),
            ("join_conversation", "leave_conversation", "cindy", [], False),
        ]
        for index, (first, second, user, members, expected) in enumerate(cases):
            with self.subTest(operation=first):
                request_type = dict(
                    add_members=conversation_pb2.AddMembers, remove_members=conversation_pb2.RemoveMembers,
                    leave_conversation=conversation_pb2.LeaveConversation,
                    join_conversation=conversation_pb2.JoinConversation)
                args = dict(conversation_id=self.conversation)
                if members:
                    args["member_ids"] = members
                first_body = request_type[first](**args)
                original = self.call(user, f"first-{index}", **{first: first_body})
                self.assertTrue(original.ack.success)
                later = self.call(user, f"later-{index}", **{second: request_type[second](**args)})
                self.assertTrue(later.ack.success)
                count = self.count()
                self.db.close()
                self.db = MiniImSqliteDb(self.path)
                self.build_services()
                replay = self.call(user, f"first-{index}", **{first: first_body})
                self.assertEqual(original.ack, replay.ack)
                self.assertEqual([], replay.sync_events)
                self.assertEqual(count, self.count())
                self.assertEqual(expected, self.repo.is_member(self.conversation, members[0] if members else user))

    def test_create_replays_do_not_append_events_or_change_current_conversation(self):
        renamed = self.call("alice", "rename", rename_conversation=conversation_pb2.RenameConversation(
            conversation_id=self.conversation, title="current"))
        self.assertTrue(renamed.ack.success)
        count = self.count()
        replay = self.call("alice", "create", create_conversation=self.create)
        self.assertEqual(self.created.ack, replay.ack)
        self.assertEqual([], replay.sync_events)
        # Legacy callers may retry client_conv_id with a new request id; business dedup remains necessary.
        retry = self.call("alice", "new-request", create_conversation=self.create)
        self.assertTrue(retry.ack.success)
        self.assertEqual(self.conversation, retry.ack.entity_id)
        self.assertEqual([], retry.sync_events)
        self.assertEqual(count, self.count())
        self.assertEqual("current", self.title())

    def test_receipt_and_recall_replay_after_membership_change_return_original_result(self):
        sent = self.messages.handle_send_message("bob", "message", message_pb2.SendMessage(
            conversation_id=self.conversation, client_msg_id="text", type=common_pb2.MSG_TEXT, content=b"text"))
        self.assertTrue(sent.ack.success)
        receipt = message_pb2.Receipt(conversation_id=self.conversation, last_read_seq=1)
        recall = message_pb2.Recall(conversation_id=self.conversation, message_id=sent.ack.entity_id)
        read_result = self.call("bob", "receipt", receipt=receipt)
        recall_result = self.call("bob", "recall", recall=recall)
        self.assertTrue(read_result.ack.success)
        self.assertTrue(recall_result.ack.success)
        removed = self.call("alice", "remove", remove_members=conversation_pb2.RemoveMembers(
            conversation_id=self.conversation, member_ids=["bob"]))
        self.assertTrue(removed.ack.success)
        count = self.count()
        self.assertEqual(read_result.ack, self.call("bob", "receipt", receipt=receipt).ack)
        self.assertEqual(recall_result.ack, self.call("bob", "recall", recall=recall).ack)
        self.assertEqual(count, self.count())
        self.assertFalse(self.repo.is_member(self.conversation, "bob"))

    def test_request_ids_are_scoped_by_user_and_bound_to_body_and_operation(self):
        rename = conversation_pb2.RenameConversation(conversation_id=self.conversation, title="alice-title")
        alice = self.call("alice", "shared", rename_conversation=rename)
        bob = self.call("bob", "shared", rename_conversation=rename)
        self.assertTrue(alice.ack.success)
        self.assertEqual(403, bob.ack.code)
        count = self.count()
        conflict = self.call("alice", "shared", add_members=conversation_pb2.AddMembers(
            conversation_id=self.conversation, member_ids=["cindy"]))
        self.assertEqual(409, conflict.ack.code)
        self.assertEqual(alice.ack, self.call("alice", "shared", rename_conversation=rename).ack)
        self.assertEqual(count, self.count())
        self.assertFalse(self.repo.is_member(self.conversation, "cindy"))
        self.assertEqual(400, self.call("alice", " ", rename_conversation=rename).ack.code)

    def test_rejected_request_stays_rejected_when_permission_later_changes(self):
        rename = conversation_pb2.RenameConversation(conversation_id=self.conversation, title="changed")
        rejected = self.call("bob", "rename", rename_conversation=rename)
        self.assertEqual(403, rejected.ack.code)
        self.db.execute_write("UPDATE conversations SET owner_id='bob' WHERE conversation_id=?", (self.conversation,))
        replay = self.call("bob", "rename", rename_conversation=rename)
        self.assertEqual(rejected.ack, replay.ack)
        self.assertEqual("original", self.title())
        self.assertTrue(self.call("bob", "new-intent", rename_conversation=rename).ack.success)
        self.assertEqual("changed", self.title())

    def test_transient_failure_rolls_back_changes_and_is_not_cached(self):
        count = self.count()
        rename = conversation_pb2.RenameConversation(conversation_id=self.conversation, title="retry")
        def transient():
            result = self.conversations.handle_rename_conversation("alice", "transient", rename)
            result.ack.success = False
            result.ack.code = 503
            return ControlWriteResult(result.ack, result.sync_events)
        result = self.requests.execute("alice", "transient", "rename_conversation", rename.SerializeToString(), transient)
        self.assertEqual(503, result.ack.code)
        self.assertEqual([], result.sync_events)
        self.assertEqual("original", self.title())
        self.assertEqual(count, self.count())
        self.assertIsNone(self.db.execute_fetchone(
            "SELECT 1 FROM control_write_results WHERE request_id='transient'"))
        self.assertTrue(self.call("alice", "transient", rename_conversation=rename).ack.success)

    def test_commit_failure_leaves_no_open_transaction_or_late_business_change(self):
        # DELETE mode makes a second reader deterministically block commit, without relying on disk exhaustion.
        self.db.m_connection.execute("PRAGMA journal_mode=DELETE")
        self.db.m_connection.execute("PRAGMA busy_timeout=0")
        blocker = sqlite3.connect(self.path)
        try:
            blocker.execute("BEGIN")
            blocker.execute("SELECT * FROM conversations").fetchall()
            rename = conversation_pb2.RenameConversation(conversation_id=self.conversation, title="after-retry")
            with self.assertRaises(sqlite3.OperationalError):
                self.call("alice", "blocked-commit", rename_conversation=rename)
            self.assertFalse(self.db.m_connection.in_transaction)
            self.assertEqual("original", self.title())
            self.assertIsNone(self.db.execute_fetchone(
                "SELECT 1 FROM control_write_results WHERE request_id='blocked-commit'"))
        finally:
            blocker.rollback()
            blocker.close()
        self.assertTrue(self.call("alice", "blocked-commit", rename_conversation=rename).ack.success)
        self.assertEqual("after-retry", self.title())

    def test_legacy_upgrade_preserves_data_and_new_results_survive_reopen(self):
        self.db.execute_write("DROP TABLE control_write_results")
        count = self.count()
        self.db.close()
        init_db(self.path)
        init_db(self.path)
        self.db = MiniImSqliteDb(self.path)
        self.build_services()
        self.assertEqual("original", self.title())
        self.assertEqual(count, self.count())
        rename = conversation_pb2.RenameConversation(conversation_id=self.conversation, title="upgraded")
        original = self.call("alice", "upgrade-write", rename_conversation=rename)
        self.assertTrue(original.ack.success)
        self.db.close()
        self.db = MiniImSqliteDb(self.path)
        self.build_services()
        count = self.count()
        self.assertEqual(original.ack, self.call("alice", "upgrade-write", rename_conversation=rename).ack)
        self.assertEqual(count, self.count())


if __name__ == "__main__":
    unittest.main()
