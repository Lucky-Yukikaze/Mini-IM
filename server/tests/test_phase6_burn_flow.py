import sys
import tempfile
import unittest
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from protocol.pb import common_pb2, conversation_pb2, message_pb2, sync_pb2
from services.conversation.service import ConversationService
from services.delivery.service import DeliveryService
from services.message.service import MessageService
from services.sync.service import SyncService
from storage.repo import ConversationRepo, DeliveryRepo, MessageRepo, SyncRepo
from storage.sqlite.db import MiniImSqliteDb


class Phase6BurnFlowTest(unittest.TestCase):
    def test_burn_message_should_cover_sender_and_reader(self):
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
                    client_conv_id="cc-burn",
                    type=common_pb2.CONVERSATION_GROUP,
                    member_ids=["u-bob"],
                    title="burn-demo",
                ),
            )
            self.assertTrue(create_result.ack.success)
            conversation_id = create_result.ack.entity_id

            send_result = message_service.handle_send_message(
                user_id="u-alice",
                request_id="req-send",
                send_message=message_pb2.SendMessage(
                    conversation_id=conversation_id,
                    client_msg_id="cm-burn-1",
                    type=common_pb2.MSG_TEXT,
                    content=b"burn me",
                    burn_mode=1,
                    burn_ttl_sec=30,
                ),
            )
            self.assertTrue(send_result.ack.success)
            message_id = send_result.ack.entity_id

            stored = message_repo.get_message_by_id(conversation_id, message_id)
            self.assertIsNotNone(stored)
            self.assertEqual(1, stored.burn_mode)
            self.assertEqual(30, stored.burn_ttl_sec)

            sender_delivery = db.execute_fetchone(
                """
                SELECT burn_started_at_ms, burn_at_ms, burned_at_ms
                FROM message_deliveries
                WHERE server_msg_id = ? AND user_id = ?
                """,
                (message_id, "u-alice"),
            )
            self.assertIsNotNone(sender_delivery)
            self.assertIsNotNone(sender_delivery["burn_started_at_ms"])
            self.assertIsNotNone(sender_delivery["burn_at_ms"])
            self.assertIsNone(sender_delivery["burned_at_ms"])

            receiver_delivery_before = db.execute_fetchone(
                """
                SELECT burn_started_at_ms, burn_at_ms
                FROM message_deliveries
                WHERE server_msg_id = ? AND user_id = ?
                """,
                (message_id, "u-bob"),
            )
            self.assertIsNotNone(receiver_delivery_before)
            self.assertIsNone(receiver_delivery_before["burn_started_at_ms"])
            self.assertIsNone(receiver_delivery_before["burn_at_ms"])

            receipt_result = delivery_service.handle_receipt(
                user_id="u-bob",
                request_id="req-receipt",
                receipt=message_pb2.Receipt(
                    conversation_id=conversation_id,
                    last_read_seq=1,
                ),
            )
            self.assertTrue(receipt_result.ack.success)

            receiver_delivery_after = db.execute_fetchone(
                """
                SELECT burn_started_at_ms, burn_at_ms, burned_at_ms
                FROM message_deliveries
                WHERE server_msg_id = ? AND user_id = ?
                """,
                (message_id, "u-bob"),
            )
            self.assertIsNotNone(receiver_delivery_after)
            self.assertIsNotNone(receiver_delivery_after["burn_started_at_ms"])
            self.assertIsNotNone(receiver_delivery_after["burn_at_ms"])
            self.assertIsNone(receiver_delivery_after["burned_at_ms"])

            sync_before_burn, cursor_before_burn = sync_service.handle_sync_request(
                user_id="u-alice",
                request=sync_pb2.SyncRequest(global_cursor=0, limit=100),
            )
            self.assertTrue(any(item.HasField("message") for item in sync_before_burn.events))

            db.execute_write(
                """
                UPDATE message_deliveries
                SET burn_at_ms = 1
                WHERE server_msg_id = ?
                """,
                (message_id,),
            )
            due_events = delivery_repo.collect_due_burn_sync_events(limit=20)
            self.assertEqual(2, len(due_events))
            self.assertEqual({"u-alice", "u-bob"}, {item.user_id for item in due_events})
            self.assertTrue(all(item.event_type == "recall" for item in due_events))

            for item in due_events:
                recall = message_pb2.Recall()
                recall.ParseFromString(item.payload)
                self.assertEqual("system-burn", recall.operator_id)
                self.assertEqual(message_id, recall.message_id)

            due_events_second = delivery_repo.collect_due_burn_sync_events(limit=20)
            self.assertEqual(0, len(due_events_second))

            alice_delta_sync, _ = sync_service.handle_sync_request(
                user_id="u-alice",
                request=sync_pb2.SyncRequest(global_cursor=cursor_before_burn, limit=100),
            )
            self.assertTrue(
                any(
                    item.HasField("recall")
                    and item.recall.message_id == message_id
                    and item.recall.operator_id == "system-burn"
                    for item in alice_delta_sync.events
                )
            )

            alice_sync, _ = sync_service.handle_sync_request(
                user_id="u-alice",
                request=sync_pb2.SyncRequest(global_cursor=0, limit=100),
            )
            bob_sync, _ = sync_service.handle_sync_request(
                user_id="u-bob",
                request=sync_pb2.SyncRequest(global_cursor=0, limit=100),
            )
            self.assertTrue(
                any(
                    item.HasField("recall")
                    and item.recall.message_id == message_id
                    and item.recall.operator_id == "system-burn"
                    for item in alice_sync.events
                )
            )
            self.assertTrue(
                any(
                    item.HasField("recall")
                    and item.recall.message_id == message_id
                    and item.recall.operator_id == "system-burn"
                    for item in bob_sync.events
                )
            )
            db.close()

    def test_manual_recall_should_block_system_burn(self):
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
                    client_conv_id="cc-recall",
                    type=common_pb2.CONVERSATION_GROUP,
                    member_ids=["u-bob"],
                    title="manual-recall-demo",
                ),
            )
            conversation_id = create_result.ack.entity_id

            send_result = message_service.handle_send_message(
                user_id="u-alice",
                request_id="req-send",
                send_message=message_pb2.SendMessage(
                    conversation_id=conversation_id,
                    client_msg_id="cm-burn-2",
                    type=common_pb2.MSG_TEXT,
                    content=b"manual recall first",
                    burn_mode=1,
                    burn_ttl_sec=30,
                ),
            )
            self.assertTrue(send_result.ack.success)
            message_id = send_result.ack.entity_id

            db.execute_write(
                """
                UPDATE message_deliveries
                SET burn_at_ms = 1
                WHERE server_msg_id = ?
                """,
                (message_id,),
            )

            recall_result = delivery_service.handle_recall(
                user_id="u-alice",
                request_id="req-recall",
                recall=message_pb2.Recall(
                    conversation_id=conversation_id,
                    message_id=message_id,
                ),
            )
            self.assertTrue(recall_result.ack.success)

            due_events = delivery_repo.collect_due_burn_sync_events(limit=20)
            self.assertEqual(0, len(due_events))

            bob_sync, _ = sync_service.handle_sync_request(
                user_id="u-bob",
                request=sync_pb2.SyncRequest(global_cursor=0, limit=100),
            )
            self.assertTrue(
                any(
                    item.HasField("recall")
                    and item.recall.message_id == message_id
                    and item.recall.operator_id == "u-alice"
                    for item in bob_sync.events
                )
            )
            self.assertFalse(
                any(
                    item.HasField("recall")
                    and item.recall.message_id == message_id
                    and item.recall.operator_id == "system-burn"
                    for item in bob_sync.events
                )
            )
            db.close()

    def test_invalid_burn_ttl_should_be_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = MiniImSqliteDb(db_path)
            db.init_schema()

            conversation_repo = ConversationRepo(db)
            conversation_service = ConversationService(conversation_repo)
            message_service = MessageService(MessageRepo(db), conversation_repo)

            create_result = conversation_service.handle_create_conversation(
                user_id="u-alice",
                request_id="req-create",
                create_conversation=conversation_pb2.CreateConversation(
                    client_conv_id="cc-invalid-ttl",
                    type=common_pb2.CONVERSATION_GROUP,
                    member_ids=["u-bob"],
                    title="invalid-ttl-demo",
                ),
            )
            conversation_id = create_result.ack.entity_id

            send_result = message_service.handle_send_message(
                user_id="u-alice",
                request_id="req-send",
                send_message=message_pb2.SendMessage(
                    conversation_id=conversation_id,
                    client_msg_id="cm-invalid-ttl",
                    type=common_pb2.MSG_TEXT,
                    content=b"invalid ttl",
                    burn_mode=1,
                    burn_ttl_sec=3,
                ),
            )
            self.assertFalse(send_result.ack.success)
            self.assertEqual(400, send_result.ack.code)
            db.close()

    def test_burn_ttl_boundary_should_be_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = MiniImSqliteDb(db_path)
            db.init_schema()

            conversation_repo = ConversationRepo(db)
            conversation_service = ConversationService(conversation_repo)
            message_service = MessageService(MessageRepo(db), conversation_repo)
            create_result = conversation_service.handle_create_conversation(
                user_id="u-alice",
                request_id="req-create",
                create_conversation=conversation_pb2.CreateConversation(
                    client_conv_id="cc-ttl-boundary",
                    type=common_pb2.CONVERSATION_GROUP,
                    member_ids=["u-bob"],
                    title="ttl-boundary",
                ),
            )
            conversation_id = create_result.ack.entity_id

            low = message_service.handle_send_message(
                user_id="u-alice",
                request_id="req-send-low",
                send_message=message_pb2.SendMessage(
                    conversation_id=conversation_id,
                    client_msg_id="cm-ttl-low",
                    type=common_pb2.MSG_TEXT,
                    content=b"low ttl",
                    burn_mode=1,
                    burn_ttl_sec=5,
                ),
            )
            high = message_service.handle_send_message(
                user_id="u-alice",
                request_id="req-send-high",
                send_message=message_pb2.SendMessage(
                    conversation_id=conversation_id,
                    client_msg_id="cm-ttl-high",
                    type=common_pb2.MSG_TEXT,
                    content=b"high ttl",
                    burn_mode=1,
                    burn_ttl_sec=604800,
                ),
            )
            self.assertTrue(low.ack.success)
            self.assertTrue(high.ack.success)
            db.close()

    def test_burn_disabled_should_not_schedule_or_emit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = MiniImSqliteDb(db_path)
            db.init_schema()

            conversation_repo = ConversationRepo(db)
            message_repo = MessageRepo(db)
            delivery_repo = DeliveryRepo(db, burn_enabled=False)
            conversation_service = ConversationService(conversation_repo)
            message_service = MessageService(message_repo, conversation_repo, burn_enabled=False)
            delivery_service = DeliveryService(delivery_repo)

            create_result = conversation_service.handle_create_conversation(
                user_id="u-alice",
                request_id="req-create",
                create_conversation=conversation_pb2.CreateConversation(
                    client_conv_id="cc-burn-disabled",
                    type=common_pb2.CONVERSATION_GROUP,
                    member_ids=["u-bob"],
                    title="burn-disabled",
                ),
            )
            conversation_id = create_result.ack.entity_id
            send_result = message_service.handle_send_message(
                user_id="u-alice",
                request_id="req-send",
                send_message=message_pb2.SendMessage(
                    conversation_id=conversation_id,
                    client_msg_id="cm-disabled",
                    type=common_pb2.MSG_TEXT,
                    content=b"burn disabled",
                    burn_mode=1,
                    burn_ttl_sec=30,
                ),
            )
            self.assertTrue(send_result.ack.success)
            message_id = send_result.ack.entity_id

            stored = message_repo.get_message_by_id(conversation_id, message_id)
            self.assertIsNotNone(stored)
            self.assertEqual(0, stored.burn_mode)
            self.assertEqual(0, stored.burn_ttl_sec)

            receipt_result = delivery_service.handle_receipt(
                user_id="u-bob",
                request_id="req-receipt",
                receipt=message_pb2.Receipt(conversation_id=conversation_id, last_read_seq=1),
            )
            self.assertTrue(receipt_result.ack.success)

            db.execute_write(
                "UPDATE message_deliveries SET burn_at_ms = 1 WHERE server_msg_id = ?",
                (message_id,),
            )
            due_events = delivery_repo.collect_due_burn_sync_events(limit=20)
            purged = delivery_repo.purge_burned_message_content(limit=20)
            self.assertEqual(0, len(due_events))
            self.assertEqual(0, purged)
            db.close()

    def test_purge_file_message_should_keep_minimal_file_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = MiniImSqliteDb(db_path)
            db.init_schema()

            conversation_repo = ConversationRepo(db)
            message_repo = MessageRepo(db)
            delivery_repo = DeliveryRepo(db)
            conversation_service = ConversationService(conversation_repo)
            message_service = MessageService(message_repo, conversation_repo)

            create_result = conversation_service.handle_create_conversation(
                user_id="u-alice",
                request_id="req-create",
                create_conversation=conversation_pb2.CreateConversation(
                    client_conv_id="cc-file-purge",
                    type=common_pb2.CONVERSATION_GROUP,
                    member_ids=["u-bob"],
                    title="file-purge",
                ),
            )
            conversation_id = create_result.ack.entity_id
            payload = json.dumps(
                {
                    "kind": "file",
                    "fileId": "f-123",
                    "fileName": "a.bin",
                    "fileSize": 9,
                },
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")

            send_result = message_service.handle_send_message(
                user_id="u-alice",
                request_id="req-send",
                send_message=message_pb2.SendMessage(
                    conversation_id=conversation_id,
                    client_msg_id="cm-file-burn",
                    type=common_pb2.MSG_FILE,
                    content=payload,
                    burn_mode=1,
                    burn_ttl_sec=30,
                ),
            )
            self.assertTrue(send_result.ack.success)
            message_id = send_result.ack.entity_id

            db.execute_write(
                "UPDATE message_deliveries SET burn_at_ms = 1 WHERE server_msg_id = ?",
                (message_id,),
            )
            events = delivery_repo.collect_due_burn_sync_events(limit=20)
            self.assertEqual(2, len(events))
            purged = delivery_repo.purge_burned_message_content(limit=20)
            self.assertEqual(1, purged)

            row = db.execute_fetchone("SELECT content FROM messages WHERE server_msg_id = ?", (message_id,))
            self.assertIsNotNone(row)
            content = row["content"]
            if isinstance(content, memoryview):
                content = content.tobytes()
            self.assertEqual(b'{"kind":"file","fileId":"f-123"}', bytes(content))
            db.close()


if __name__ == "__main__":
    unittest.main()
