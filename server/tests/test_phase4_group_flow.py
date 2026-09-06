import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from protocol.pb import common_pb2, conversation_pb2, message_pb2, sync_pb2
from services.conversation.service import ConversationService
from services.delivery.service import DeliveryService
from services.message.service import MessageService
from services.sync.service import SyncService
from storage.repo import ConversationRepo, DeliveryRepo, MessageRepo, SyncRepo
from storage.sqlite.db import MiniImSqliteDb


class Phase4GroupFlowTest(unittest.TestCase):
    def test_group_message_receipt_and_recall_can_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = MiniImSqliteDb(db_path)
            db.init_schema()

            conversation_repo = ConversationRepo(db)
            message_repo = MessageRepo(db)
            delivery_repo = DeliveryRepo(db)
            conversation_service = ConversationService(conversation_repo)
            message_service = MessageService(message_repo, conversation_repo)
            delivery_service = DeliveryService(delivery_repo)
            sync_service = SyncService(SyncRepo(db))

            create_result = conversation_service.handle_create_conversation(
                user_id="u-alice",
                request_id="req-create",
                create_conversation=conversation_pb2.CreateConversation(
                    client_conv_id="cc-1",
                    type=common_pb2.CONVERSATION_GROUP,
                    member_ids=["u-bob", "u-cindy"],
                    title="demo-group",
                ),
            )
            self.assertTrue(create_result.ack.success)
            conversation_id = create_result.ack.entity_id

            send_result = message_service.handle_send_message(
                user_id="u-alice",
                request_id="req-send",
                send_message=message_pb2.SendMessage(
                    conversation_id=conversation_id,
                    client_msg_id="cm-1",
                    type=common_pb2.MSG_TEXT,
                    content=b"hello-group",
                ),
            )
            self.assertTrue(send_result.ack.success)
            self.assertIsNotNone(send_result.message_push)
            message_id = send_result.ack.entity_id
            self.assertEqual(2, send_result.message_push.messages[0].unread_count)

            receipt_result = delivery_service.handle_receipt(
                user_id="u-bob",
                request_id="req-receipt",
                receipt=message_pb2.Receipt(
                    conversation_id=conversation_id,
                    last_read_seq=1,
                ),
            )
            self.assertTrue(receipt_result.ack.success)

            stored_after_receipt = message_repo.get_message_by_id(conversation_id, message_id)
            self.assertIsNotNone(stored_after_receipt)
            self.assertEqual(1, stored_after_receipt.unread_count)

            recall_result = delivery_service.handle_recall(
                user_id="u-alice",
                request_id="req-recall",
                recall=message_pb2.Recall(
                    conversation_id=conversation_id,
                    message_id=message_id,
                ),
            )
            self.assertTrue(recall_result.ack.success)

            alice_sync, _ = sync_service.handle_sync_request(
                user_id="u-alice",
                request=sync_pb2.SyncRequest(global_cursor=0, limit=20),
            )
            bob_sync, _ = sync_service.handle_sync_request(
                user_id="u-bob",
                request=sync_pb2.SyncRequest(global_cursor=0, limit=20),
            )

            self.assertTrue(any(item.HasField("conversation_updated") for item in alice_sync.events))
            self.assertTrue(any(item.HasField("message") for item in alice_sync.events))
            self.assertTrue(any(item.HasField("receipt") for item in alice_sync.events))
            self.assertTrue(any(item.HasField("recall") for item in alice_sync.events))
            self.assertTrue(any(item.HasField("receipt") for item in bob_sync.events))

            recalled_message = message_repo.get_message_by_id(conversation_id, message_id)
            self.assertIsNotNone(recalled_message)
            self.assertTrue(recalled_message.recalled)
            db.close()

    def test_group_members_can_change_and_removed_member_cannot_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = MiniImSqliteDb(db_path)
            db.init_schema()

            conversation_repo = ConversationRepo(db)
            message_repo = MessageRepo(db)
            conversation_service = ConversationService(conversation_repo)
            message_service = MessageService(message_repo, conversation_repo)
            sync_service = SyncService(SyncRepo(db))

            create_result = conversation_service.handle_create_conversation(
                user_id="u-alice",
                request_id="req-create",
                create_conversation=conversation_pb2.CreateConversation(
                    client_conv_id="cc-2",
                    type=common_pb2.CONVERSATION_GROUP,
                    member_ids=["u-bob"],
                    title="demo-group",
                ),
            )
            self.assertTrue(create_result.ack.success)
            conversation_id = create_result.ack.entity_id

            conversation_repo.ensure_user("u-cindy")
            add_result = conversation_service.handle_add_members(
                user_id="u-alice",
                request_id="req-add",
                add_members=conversation_pb2.AddMembers(
                    conversation_id=conversation_id,
                    member_ids=["u-cindy"],
                ),
            )
            self.assertTrue(add_result.ack.success)
            self.assertIn("u-cindy", add_result.conversation_updated.member_ids)

            rename_result = conversation_service.handle_rename_conversation(
                user_id="u-alice",
                request_id="req-rename",
                rename_conversation=conversation_pb2.RenameConversation(
                    conversation_id=conversation_id,
                    title="renamed-group",
                ),
            )
            self.assertTrue(rename_result.ack.success)
            self.assertEqual("renamed-group", rename_result.conversation_updated.title)

            remove_result = conversation_service.handle_remove_members(
                user_id="u-alice",
                request_id="req-remove",
                remove_members=conversation_pb2.RemoveMembers(
                    conversation_id=conversation_id,
                    member_ids=["u-bob"],
                ),
            )
            self.assertTrue(remove_result.ack.success)
            self.assertNotIn("u-bob", remove_result.conversation_updated.member_ids)

            rejected_send = message_service.handle_send_message(
                user_id="u-bob",
                request_id="req-send-removed",
                send_message=message_pb2.SendMessage(
                    conversation_id=conversation_id,
                    client_msg_id="cm-removed",
                    type=common_pb2.MSG_TEXT,
                    content=b"blocked",
                ),
            )
            self.assertFalse(rejected_send.ack.success)

            leave_result = conversation_service.handle_leave_conversation(
                user_id="u-cindy",
                request_id="req-leave",
                leave_conversation=conversation_pb2.LeaveConversation(conversation_id=conversation_id),
            )
            self.assertTrue(leave_result.ack.success)
            self.assertNotIn("u-cindy", leave_result.conversation_updated.member_ids)

            cindy_sync, _ = sync_service.handle_sync_request(
                user_id="u-cindy",
                request=sync_pb2.SyncRequest(global_cursor=0, limit=20),
            )
            self.assertTrue(any(item.HasField("conversation_updated") for item in cindy_sync.events))
            self.assertFalse(conversation_repo.is_member(conversation_id, "u-bob"))
            self.assertFalse(conversation_repo.is_member(conversation_id, "u-cindy"))
            db.close()

    def test_direct_conversation_can_send_and_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = MiniImSqliteDb(db_path)
            db.init_schema()

            conversation_repo = ConversationRepo(db)
            message_repo = MessageRepo(db)
            conversation_service = ConversationService(conversation_repo)
            message_service = MessageService(message_repo, conversation_repo)
            sync_service = SyncService(SyncRepo(db))

            create_result = conversation_service.handle_create_conversation(
                user_id="u-alice",
                request_id="req-direct",
                create_conversation=conversation_pb2.CreateConversation(
                    client_conv_id="dc-1",
                    type=common_pb2.CONVERSATION_DIRECT,
                    member_ids=["u-bob"],
                    title="",
                ),
            )
            self.assertTrue(create_result.ack.success)
            conversation_id = create_result.ack.entity_id
            self.assertEqual(common_pb2.CONVERSATION_DIRECT, create_result.conversation_updated.type)
            self.assertEqual(["u-alice", "u-bob"], list(create_result.conversation_updated.member_ids))

            send_result = message_service.handle_send_message(
                user_id="u-alice",
                request_id="req-direct-send",
                send_message=message_pb2.SendMessage(
                    conversation_id=conversation_id,
                    client_msg_id="cm-direct",
                    type=common_pb2.MSG_TEXT,
                    content=b"hello-bob",
                ),
            )
            self.assertTrue(send_result.ack.success)

            bob_sync, _ = sync_service.handle_sync_request(
                user_id="u-bob",
                request=sync_pb2.SyncRequest(global_cursor=0, limit=20),
            )
            self.assertTrue(any(item.HasField("conversation_updated") for item in bob_sync.events))
            self.assertTrue(any(item.HasField("message") for item in bob_sync.events))
            db.close()

    def test_user_can_join_group_by_conversation_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = MiniImSqliteDb(db_path)
            db.init_schema()

            conversation_repo = ConversationRepo(db)
            message_repo = MessageRepo(db)
            conversation_service = ConversationService(conversation_repo)
            message_service = MessageService(message_repo, conversation_repo)
            sync_service = SyncService(SyncRepo(db))

            create_result = conversation_service.handle_create_conversation(
                user_id="u-alice",
                request_id="req-create-joinable",
                create_conversation=conversation_pb2.CreateConversation(
                    client_conv_id="cc-joinable",
                    type=common_pb2.CONVERSATION_GROUP,
                    member_ids=["u-bob"],
                    title="joinable-group",
                ),
            )
            self.assertTrue(create_result.ack.success)
            conversation_id = create_result.ack.entity_id

            conversation_repo.ensure_user("u-cindy")
            join_result = conversation_service.handle_join_conversation(
                user_id="u-cindy",
                request_id="req-join",
                join_conversation=conversation_pb2.JoinConversation(conversation_id=conversation_id),
            )
            self.assertTrue(join_result.ack.success)
            self.assertIn("u-cindy", join_result.conversation_updated.member_ids)
            self.assertTrue(conversation_repo.is_member(conversation_id, "u-cindy"))

            idempotent_join = conversation_service.handle_join_conversation(
                user_id="u-cindy",
                request_id="req-join-again",
                join_conversation=conversation_pb2.JoinConversation(conversation_id=conversation_id),
            )
            self.assertTrue(idempotent_join.ack.success)
            self.assertEqual("ok(idempotent)", idempotent_join.ack.message)

            send_result = message_service.handle_send_message(
                user_id="u-cindy",
                request_id="req-cindy-send",
                send_message=message_pb2.SendMessage(
                    conversation_id=conversation_id,
                    client_msg_id="cm-cindy-join",
                    type=common_pb2.MSG_TEXT,
                    content=b"joined",
                ),
            )
            self.assertTrue(send_result.ack.success)

            cindy_sync, _ = sync_service.handle_sync_request(
                user_id="u-cindy",
                request=sync_pb2.SyncRequest(global_cursor=0, limit=20),
            )
            self.assertTrue(any(item.HasField("conversation_updated") for item in cindy_sync.events))
            self.assertTrue(any(item.HasField("message") for item in cindy_sync.events))
            db.close()

    def test_unknown_user_cannot_be_invited_or_start_direct_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = MiniImSqliteDb(db_path)
            db.init_schema()

            conversation_repo = ConversationRepo(db)
            conversation_service = ConversationService(conversation_repo)
            conversation_repo.ensure_user("u-alice")
            conversation_repo.ensure_user("u-bob")

            create_result = conversation_service.handle_create_conversation(
                user_id="u-alice",
                request_id="req-create-known",
                create_conversation=conversation_pb2.CreateConversation(
                    client_conv_id="cc-known",
                    type=common_pb2.CONVERSATION_GROUP,
                    member_ids=["u-bob"],
                    title="known-group",
                ),
            )
            self.assertTrue(create_result.ack.success)

            add_unknown = conversation_service.handle_add_members(
                user_id="u-alice",
                request_id="req-add-unknown",
                add_members=conversation_pb2.AddMembers(
                    conversation_id=create_result.ack.entity_id,
                    member_ids=["u-not-exist"],
                ),
            )
            self.assertFalse(add_unknown.ack.success)

            direct_unknown = conversation_service.handle_create_conversation(
                user_id="u-alice",
                request_id="req-direct-unknown",
                create_conversation=conversation_pb2.CreateConversation(
                    client_conv_id="dc-unknown",
                    type=common_pb2.CONVERSATION_DIRECT,
                    member_ids=["u-not-exist"],
                ),
            )
            self.assertFalse(direct_unknown.ack.success)
            db.close()


if __name__ == "__main__":
    unittest.main()
