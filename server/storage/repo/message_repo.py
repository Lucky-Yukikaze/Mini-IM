from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from protocol.pb import message_pb2
from storage.repo.sync_event import AppendSyncEvents, StoredSyncEvent
from storage.sqlite.db import MiniImSqliteDb


@dataclass
class StoredMessage:
    message: message_pb2.Message
    sync_events: list[StoredSyncEvent]


class MessageRepo:
    BURN_MODE_AFTER_READ = 1

    def __init__(self, db: MiniImSqliteDb) -> None:
        self.m_db = db

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    @classmethod
    def _compute_burn_at_ms(cls, burn_mode: int, burn_ttl_sec: int, started_at_ms: int) -> int | None:
        if int(burn_mode) != cls.BURN_MODE_AFTER_READ or int(burn_ttl_sec) <= 0:
            return None
        return int(started_at_ms) + int(burn_ttl_sec) * 1000

    def get_message_by_client_msg_id(
        self,
        conversation_id: str,
        sender_id: str,
        client_msg_id: str,
    ) -> message_pb2.Message | None:
        row = self.m_db.execute_fetchone(
            """
            SELECT
              m.server_msg_id,
              m.conversation_id,
              m.sender_id,
              m.client_msg_id,
              m.conversation_seq,
              m.type,
              m.content,
              m.created_at_ms,
              m.recalled,
              m.burn_mode,
              m.burn_ttl_sec,
              COALESCE(c.unread_count, 0) AS unread_count
            FROM messages AS m
            LEFT JOIN message_read_counters AS c
              ON c.server_msg_id = m.server_msg_id
            WHERE m.conversation_id = ? AND m.sender_id = ? AND m.client_msg_id = ?
            """,
            (conversation_id, sender_id, client_msg_id),
        )
        if row is None:
            return None
        return self._row_to_message(row)

    def get_message_by_id(self, conversation_id: str, message_id: str) -> message_pb2.Message | None:
        row = self.m_db.execute_fetchone(
            """
            SELECT
              m.server_msg_id,
              m.conversation_id,
              m.sender_id,
              m.client_msg_id,
              m.conversation_seq,
              m.type,
              m.content,
              m.created_at_ms,
              m.recalled,
              m.burn_mode,
              m.burn_ttl_sec,
              COALESCE(c.unread_count, 0) AS unread_count
            FROM messages AS m
            LEFT JOIN message_read_counters AS c
              ON c.server_msg_id = m.server_msg_id
            WHERE m.conversation_id = ? AND m.server_msg_id = ?
            """,
            (conversation_id, message_id),
        )
        if row is None:
            return None
        return self._row_to_message(row)

    def append_message(
        self,
        request_id: str,
        sender_id: str,
        send_message: message_pb2.SendMessage,
        member_ids: list[str],
    ) -> StoredMessage:
        now_ms = self._now_ms()
        message_id = str(uuid.uuid4())
        unread_member_count = sum(1 for member_id in member_ids if member_id != sender_id)
        burn_mode = int(send_message.burn_mode)
        burn_ttl_sec = int(send_message.burn_ttl_sec)

        connection = self.m_db.m_connection
        with self.m_db.transaction():
            next_seq_row = connection.execute(
                "SELECT COALESCE(MAX(conversation_seq), 0) + 1 AS next_seq FROM messages WHERE conversation_id = ?",
                (send_message.conversation_id,),
            ).fetchone()
            conversation_seq = int(next_seq_row["next_seq"])

            connection.execute(
                """
                INSERT INTO messages(
                  server_msg_id,
                  conversation_id,
                  sender_id,
                  client_msg_id,
                  request_id,
                  conversation_seq,
                  type,
                  content,
                  created_at_ms,
                  recalled,
                  burn_mode,
                  burn_ttl_sec
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    message_id,
                    send_message.conversation_id,
                    sender_id,
                    send_message.client_msg_id,
                    request_id,
                    conversation_seq,
                    int(send_message.type),
                    bytes(send_message.content),
                    now_ms,
                    burn_mode,
                    burn_ttl_sec,
                ),
            )

            for member_id in member_ids:
                if member_id == sender_id:
                    status = "read"
                    read_at_ms = now_ms
                    burn_at_ms = self._compute_burn_at_ms(burn_mode, burn_ttl_sec, now_ms)
                    burn_started_at_ms = now_ms if burn_at_ms is not None else None
                else:
                    status = "sent"
                    read_at_ms = None
                    burn_started_at_ms = None
                    burn_at_ms = None
                connection.execute(
                    """
                    INSERT OR REPLACE INTO message_deliveries(
                      server_msg_id,
                      user_id,
                      conversation_id,
                      seq,
                      status,
                      sent_at_ms,
                      delivered_at_ms,
                      read_at_ms,
                      burn_started_at_ms,
                      burn_at_ms,
                      burned_at_ms,
                      failed_at_ms,
                      failure_reason
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                    """,
                    (
                        message_id,
                        member_id,
                        send_message.conversation_id,
                        conversation_seq,
                        status,
                        now_ms,
                        now_ms if member_id == sender_id else None,
                        read_at_ms,
                        burn_started_at_ms,
                        burn_at_ms,
                    ),
                )

            connection.execute(
                """
                INSERT INTO message_read_counters(
                  server_msg_id,
                  conversation_id,
                  conversation_seq,
                  member_count,
                  read_count,
                  unread_count,
                  updated_at_ms
                ) VALUES(?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    message_id,
                    send_message.conversation_id,
                    conversation_seq,
                    max(unread_member_count, 0),
                    max(unread_member_count, 0),
                    now_ms,
                ),
            )

            message = message_pb2.Message(
                message_id=message_id,
                conversation_id=send_message.conversation_id,
                sender_id=sender_id,
                client_msg_id=send_message.client_msg_id,
                seq=conversation_seq,
                type=send_message.type,
                content=send_message.content,
                created_at_ms=now_ms,
                recalled=False,
                unread_count=max(unread_member_count, 0),
                burn_mode=burn_mode,
                burn_ttl_sec=burn_ttl_sec,
            )

            sync_events = AppendSyncEvents(
                connection, member_ids, send_message.conversation_id, "message", message, now_ms,
            )
            for member_id in member_ids:
                connection.execute(
                    """
                    INSERT INTO sync_cursors(user_id, conversation_id, last_seq, updated_at_ms)
                    VALUES(?, ?, ?, ?)
                    ON CONFLICT(user_id, conversation_id)
                    DO UPDATE SET last_seq = excluded.last_seq, updated_at_ms = excluded.updated_at_ms
                    """,
                    (member_id, send_message.conversation_id, conversation_seq, now_ms),
                )

        return StoredMessage(message=message, sync_events=sync_events)

    @staticmethod
    def _row_to_message(row) -> message_pb2.Message:
        content = row["content"]
        if isinstance(content, memoryview):
            content = content.tobytes()
        return message_pb2.Message(
            message_id=str(row["server_msg_id"]),
            conversation_id=str(row["conversation_id"]),
            sender_id=str(row["sender_id"]),
            client_msg_id=str(row["client_msg_id"]),
            seq=int(row["conversation_seq"]),
            type=int(row["type"]),
            content=bytes(content),
            created_at_ms=int(row["created_at_ms"]),
            recalled=bool(row["recalled"]),
            unread_count=int(row["unread_count"]),
            burn_mode=int(row["burn_mode"]),
            burn_ttl_sec=int(row["burn_ttl_sec"]),
        )
