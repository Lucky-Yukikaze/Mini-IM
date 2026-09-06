import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from protocol.pb import common_pb2, conversation_pb2, message_pb2, sync_pb2
from protocol.pb.auth_pb2 import Hello
from services.auth.service import AuthService
from services.conversation.service import ConversationService
from services.message.service import MessageService
from services.sync.service import SyncService
from storage.repo import ConversationRepo, MessageRepo, SyncRepo
from storage.sqlite.db import MiniImSqliteDb


class MultiUserDevFlowTest(unittest.TestCase):
    def _build_services(self, db: MiniImSqliteDb) -> tuple[ConversationRepo, ConversationService, MessageService, SyncService]:
        conversation_repo = ConversationRepo(db)
        conversation_service = ConversationService(conversation_repo)
        message_service = MessageService(MessageRepo(db), conversation_repo)
        sync_service = SyncService(SyncRepo(db))
        return conversation_repo, conversation_service, message_service, sync_service

    def test_dev_users_can_group_chat_and_direct_chat(self):
        auth_service = AuthService()
        alice = auth_service.handle_hello(Hello(token="dev-token:u-alice", device_id="alice-device"))
        bob = auth_service.handle_hello(Hello(token="dev-token:u-bob", device_id="bob-device"))
        cindy = auth_service.handle_hello(Hello(token="dev-token:u-cindy", device_id="cindy-device"))

        self.assertEqual("u-alice", alice.user_id)
        self.assertEqual("u-bob", bob.user_id)
        self.assertEqual("u-cindy", cindy.user_id)
        self.assertNotEqual(alice.session_id, bob.session_id)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = MiniImSqliteDb(db_path)
            db.init_schema()
            conversation_repo, conversation_service, message_service, sync_service = self._build_services(db)

            group_result = conversation_service.handle_create_conversation(
                user_id=alice.user_id,
                request_id="req-group",
                create_conversation=conversation_pb2.CreateConversation(
                    client_conv_id="cc-group",
                    type=common_pb2.CONVERSATION_GROUP,
                    member_ids=[bob.user_id, cindy.user_id],
                    title="三人群",
                ),
            )
            self.assertTrue(group_result.ack.success)
            group_id = group_result.ack.entity_id

            group_message = message_service.handle_send_message(
                user_id=alice.user_id,
                request_id="req-group-msg",
                send_message=message_pb2.SendMessage(
                    conversation_id=group_id,
                    client_msg_id="cm-group-1",
                    type=common_pb2.MSG_TEXT,
                    content=b"hello group",
                ),
            )
            self.assertTrue(group_message.ack.success)

            bob_group_sync, _ = sync_service.handle_sync_request(
                user_id=bob.user_id,
                request=sync_pb2.SyncRequest(global_cursor=0, limit=20),
            )
            self.assertTrue(any(item.HasField("conversation_updated") for item in bob_group_sync.events))
            self.assertTrue(any(item.HasField("message") for item in bob_group_sync.events))

            remove_cindy = conversation_service.handle_remove_members(
                user_id=alice.user_id,
                request_id="req-remove-cindy",
                remove_members=conversation_pb2.RemoveMembers(
                    conversation_id=group_id,
                    member_ids=[cindy.user_id],
                ),
            )
            self.assertTrue(remove_cindy.ack.success)
            self.assertFalse(conversation_repo.is_member(group_id, cindy.user_id))

            cindy_rejected = message_service.handle_send_message(
                user_id=cindy.user_id,
                request_id="req-cindy-blocked",
                send_message=message_pb2.SendMessage(
                    conversation_id=group_id,
                    client_msg_id="cm-cindy-blocked",
                    type=common_pb2.MSG_TEXT,
                    content=b"should not send",
                ),
            )
            self.assertFalse(cindy_rejected.ack.success)
            self.assertEqual(403, cindy_rejected.ack.code)

            bob_leave = conversation_service.handle_leave_conversation(
                user_id=bob.user_id,
                request_id="req-bob-leave",
                leave_conversation=conversation_pb2.LeaveConversation(conversation_id=group_id),
            )
            self.assertTrue(bob_leave.ack.success)
            self.assertFalse(conversation_repo.is_member(group_id, bob.user_id))

            bob_rejected = message_service.handle_send_message(
                user_id=bob.user_id,
                request_id="req-bob-blocked",
                send_message=message_pb2.SendMessage(
                    conversation_id=group_id,
                    client_msg_id="cm-bob-blocked",
                    type=common_pb2.MSG_TEXT,
                    content=b"should not send",
                ),
            )
            self.assertFalse(bob_rejected.ack.success)
            self.assertEqual(403, bob_rejected.ack.code)

            direct_result = conversation_service.handle_create_conversation(
                user_id=alice.user_id,
                request_id="req-direct",
                create_conversation=conversation_pb2.CreateConversation(
                    client_conv_id="cc-direct",
                    type=common_pb2.CONVERSATION_DIRECT,
                    member_ids=[bob.user_id],
                ),
            )
            self.assertTrue(direct_result.ack.success)
            direct_id = direct_result.ack.entity_id

            direct_message = message_service.handle_send_message(
                user_id=alice.user_id,
                request_id="req-direct-msg",
                send_message=message_pb2.SendMessage(
                    conversation_id=direct_id,
                    client_msg_id="cm-direct-1",
                    type=common_pb2.MSG_TEXT,
                    content=b"hello bob",
                ),
            )
            self.assertTrue(direct_message.ack.success)

            bob_direct_sync, _ = sync_service.handle_sync_request(
                user_id=bob.user_id,
                request=sync_pb2.SyncRequest(global_cursor=0, limit=50, conversation_id=direct_id),
            )
            self.assertTrue(any(item.HasField("conversation_updated") for item in bob_direct_sync.events))
            self.assertTrue(any(item.HasField("message") for item in bob_direct_sync.events))
            db.close()


if __name__ == "__main__":
    unittest.main()
