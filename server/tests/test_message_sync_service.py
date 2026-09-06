import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from protocol.pb import common_pb2, conversation_pb2, message_pb2, sync_pb2
from services.conversation.service import ConversationService
from services.message.service import MessageService
from services.sync.service import SyncService
from storage.repo import ConversationRepo, MessageRepo, SyncRepo
from storage.sqlite.db import MiniImSqliteDb


class MessageSyncServiceTest(unittest.TestCase):
    def test_send_message_then_sync_can_fetch_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = MiniImSqliteDb(db_path)
            try:
                db.init_schema()

                conversation_repo = ConversationRepo(db)
                conversation_service = ConversationService(conversation_repo)
                create_result = conversation_service.handle_create_conversation(
                    user_id="u-demo",
                    request_id="req-create",
                    create_conversation=conversation_pb2.CreateConversation(
                        client_conv_id="cc-demo",
                        type=common_pb2.CONVERSATION_GROUP,
                        member_ids=["u-peer"],
                        title="demo",
                    ),
                )
                message_service = MessageService(MessageRepo(db), conversation_repo)
                sync_service = SyncService(SyncRepo(db))
                conversation_id = create_result.ack.entity_id

                send_message = message_pb2.SendMessage(
                    conversation_id=conversation_id,
                    client_msg_id="cm-1",
                    type=common_pb2.MSG_TEXT,
                    content=b"hello",
                )
                result = message_service.handle_send_message(
                    user_id="u-demo",
                    request_id="req-1",
                    send_message=send_message,
                )

                self.assertTrue(result.ack.success)
                self.assertEqual("ok", result.ack.message)
                self.assertIsNotNone(result.message_push)
                self.assertEqual(1, len(result.message_push.messages))

                sync_response, new_cursor = sync_service.handle_sync_request(
                    user_id="u-demo",
                    request=sync_pb2.SyncRequest(global_cursor=0, limit=20),
                )
                self.assertGreaterEqual(len(sync_response.events), 2)
                self.assertEqual(new_cursor, sync_response.new_global_cursor)
                self.assertTrue(any(item.HasField("conversation_updated") for item in sync_response.events))
                self.assertTrue(any(item.HasField("message") for item in sync_response.events))
                message_event = next(item for item in sync_response.events if item.HasField("message"))
                self.assertEqual("hello", message_event.message.content.decode("utf-8"))
            finally:
                db.close()

    def test_send_message_is_idempotent_by_client_msg_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = MiniImSqliteDb(db_path)
            try:
                db.init_schema()

                conversation_repo = ConversationRepo(db)
                conversation_service = ConversationService(conversation_repo)
                create_result = conversation_service.handle_create_conversation(
                    user_id="u-demo",
                    request_id="req-create",
                    create_conversation=conversation_pb2.CreateConversation(
                        client_conv_id="cc-demo",
                        type=common_pb2.CONVERSATION_GROUP,
                        member_ids=["u-peer"],
                        title="demo",
                    ),
                )
                message_service = MessageService(MessageRepo(db), conversation_repo)
                sync_service = SyncService(SyncRepo(db))
                conversation_id = create_result.ack.entity_id

                send_message = message_pb2.SendMessage(
                    conversation_id=conversation_id,
                    client_msg_id="cm-dup",
                    type=common_pb2.MSG_TEXT,
                    content=b"once",
                )
                first = message_service.handle_send_message(
                    user_id="u-demo",
                    request_id="req-1",
                    send_message=send_message,
                )
                second = message_service.handle_send_message(
                    user_id="u-demo",
                    request_id="req-2",
                    send_message=send_message,
                )

                self.assertTrue(first.ack.success)
                self.assertTrue(second.ack.success)
                self.assertEqual(first.ack.entity_id, second.ack.entity_id)
                self.assertIsNone(second.message_push)

                sync_response, _ = sync_service.handle_sync_request(
                    user_id="u-demo",
                    request=sync_pb2.SyncRequest(global_cursor=0, limit=20),
                )
                self.assertEqual(1, len([item for item in sync_response.events if item.HasField("message")]))
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
