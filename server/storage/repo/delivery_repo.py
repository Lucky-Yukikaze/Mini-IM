from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

from protocol.pb import common_pb2, message_pb2
from storage.repo.conversation_repo import StoredSyncEvent
from storage.sqlite.db import MiniImSqliteDb


@dataclass
class ReceiptApplyResult:
    receipt: message_pb2.Receipt
    sync_events: list[StoredSyncEvent]
    updated: bool


@dataclass
class RecallApplyResult:
    recall: message_pb2.Recall
    sync_events: list[StoredSyncEvent]
    updated: bool


class DeliveryRepo:
    BURN_MODE_AFTER_READ = 1

    def __init__(self, db: MiniImSqliteDb, burn_enabled: bool = True) -> None:
        self.m_db = db
        self.m_burn_enabled = bool(burn_enabled)

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _next_global_seq(connection, user_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM sync_events WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return int(row["next_seq"])

    def apply_receipt(self, user_id: str, conversation_id: str, last_read_seq: int) -> ReceiptApplyResult | None:
        now_ms = self._now_ms()
        connection = self.m_db.m_connection

        with connection:
            member_row = connection.execute(
                """
                SELECT last_read_seq
                FROM conversation_members
                WHERE conversation_id = ? AND user_id = ?
                """,
                (conversation_id, user_id),
            ).fetchone()
            if member_row is None:
                return None

            max_seq_row = connection.execute(
                """
                SELECT COALESCE(MAX(conversation_seq), 0) AS max_seq
                FROM messages
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()

            old_last_read_seq = int(member_row["last_read_seq"])
            max_seq = int(max_seq_row["max_seq"])
            new_last_read_seq = max(old_last_read_seq, min(int(last_read_seq), max_seq))
            receipt = message_pb2.Receipt(
                event_id="",
                conversation_id=conversation_id,
                last_read_seq=new_last_read_seq,
                read_at_ms=now_ms,
                reader_id=user_id,
            )
            if new_last_read_seq <= old_last_read_seq:
                return ReceiptApplyResult(receipt=receipt, sync_events=[], updated=False)

            connection.execute(
                """
                UPDATE conversation_members
                SET last_read_seq = ?
                WHERE conversation_id = ? AND user_id = ?
                """,
                (new_last_read_seq, conversation_id, user_id),
            )
            connection.execute(
                """
                UPDATE message_deliveries
                SET status = 'read', read_at_ms = COALESCE(read_at_ms, ?)
                WHERE user_id = ? AND conversation_id = ? AND seq > ? AND seq <= ?
                """,
                (now_ms, user_id, conversation_id, old_last_read_seq, new_last_read_seq),
            )
            if self.m_burn_enabled:
                connection.execute(
                    """
                    UPDATE message_deliveries
                    SET
                      burn_started_at_ms = COALESCE(burn_started_at_ms, ?),
                      burn_at_ms = COALESCE(
                        burn_at_ms,
                        ? + (
                          SELECT m.burn_ttl_sec * 1000
                          FROM messages AS m
                          WHERE m.server_msg_id = message_deliveries.server_msg_id
                        )
                      )
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND seq > ?
                      AND seq <= ?
                      AND burned_at_ms IS NULL
                      AND burn_started_at_ms IS NULL
                      AND server_msg_id IN (
                        SELECT server_msg_id
                        FROM messages
                        WHERE conversation_id = ?
                          AND conversation_seq > ?
                          AND conversation_seq <= ?
                          AND sender_id <> ?
                          AND burn_mode = ?
                          AND burn_ttl_sec > 0
                      )
                    """,
                    (
                        now_ms,
                        now_ms,
                        user_id,
                        conversation_id,
                        old_last_read_seq,
                        new_last_read_seq,
                        conversation_id,
                        old_last_read_seq,
                        new_last_read_seq,
                        user_id,
                        self.BURN_MODE_AFTER_READ,
                    ),
                )
            connection.execute(
                """
                UPDATE message_read_counters
                SET
                  read_count = CASE
                    WHEN read_count < member_count THEN read_count + 1
                    ELSE member_count
                  END,
                  unread_count = CASE
                    WHEN unread_count > 0 THEN unread_count - 1
                    ELSE 0
                  END,
                  updated_at_ms = ?
                WHERE server_msg_id IN (
                  SELECT server_msg_id
                  FROM messages
                  WHERE conversation_id = ?
                    AND conversation_seq > ?
                    AND conversation_seq <= ?
                    AND sender_id <> ?
                )
                """,
                (now_ms, conversation_id, old_last_read_seq, new_last_read_seq, user_id),
            )

            member_rows = connection.execute(
                """
                SELECT user_id
                FROM conversation_members
                WHERE conversation_id = ?
                ORDER BY joined_at_ms ASC, user_id ASC
                """,
                (conversation_id,),
            ).fetchall()

            payload_event_id = str(uuid.uuid4())
            receipt.event_id = payload_event_id
            payload = receipt.SerializeToString()
            sync_events: list[StoredSyncEvent] = []
            for row in member_rows:
                member_id = str(row["user_id"])
                event_id = str(uuid.uuid4())
                global_seq = self._next_global_seq(connection, member_id)
                connection.execute(
                    """
                    INSERT INTO sync_events(event_id, user_id, seq, conversation_id, event_type, payload, created_at_ms)
                    VALUES(?, ?, ?, ?, 'receipt', ?, ?)
                    """,
                    (event_id, member_id, global_seq, conversation_id, payload, now_ms),
                )
                sync_events.append(
                    StoredSyncEvent(
                        user_id=member_id,
                        global_seq=global_seq,
                        event_id=payload_event_id,
                        event_type="receipt",
                        payload=payload,
                    )
                )

        return ReceiptApplyResult(receipt=receipt, sync_events=sync_events, updated=True)

    def apply_recall(self, user_id: str, conversation_id: str, message_id: str) -> RecallApplyResult | None:
        now_ms = self._now_ms()
        connection = self.m_db.m_connection

        with connection:
            message_row = connection.execute(
                """
                SELECT sender_id, recalled
                FROM messages
                WHERE server_msg_id = ? AND conversation_id = ?
                """,
                (message_id, conversation_id),
            ).fetchone()
            if message_row is None:
                return None
            if str(message_row["sender_id"]) != user_id:
                return None

            recall = message_pb2.Recall(
                event_id="",
                conversation_id=conversation_id,
                message_id=message_id,
                ts_ms=now_ms,
                operator_id=user_id,
            )
            if bool(message_row["recalled"]):
                return RecallApplyResult(recall=recall, sync_events=[], updated=False)

            connection.execute(
                """
                UPDATE messages
                SET recalled = 1
                WHERE server_msg_id = ? AND conversation_id = ?
                """,
                (message_id, conversation_id),
            )
            connection.execute(
                """
                UPDATE message_deliveries
                SET burned_at_ms = COALESCE(burned_at_ms, ?)
                WHERE server_msg_id = ?
                """,
                (now_ms, message_id),
            )

            member_rows = connection.execute(
                """
                SELECT user_id
                FROM conversation_members
                WHERE conversation_id = ?
                ORDER BY joined_at_ms ASC, user_id ASC
                """,
                (conversation_id,),
            ).fetchall()

            payload_event_id = str(uuid.uuid4())
            recall.event_id = payload_event_id
            payload = recall.SerializeToString()
            sync_events: list[StoredSyncEvent] = []
            for row in member_rows:
                member_id = str(row["user_id"])
                event_id = str(uuid.uuid4())
                global_seq = self._next_global_seq(connection, member_id)
                connection.execute(
                    """
                    INSERT INTO sync_events(event_id, user_id, seq, conversation_id, event_type, payload, created_at_ms)
                    VALUES(?, ?, ?, ?, 'recall', ?, ?)
                    """,
                    (event_id, member_id, global_seq, conversation_id, payload, now_ms),
                )
                sync_events.append(
                    StoredSyncEvent(
                        user_id=member_id,
                        global_seq=global_seq,
                        event_id=payload_event_id,
                        event_type="recall",
                        payload=payload,
                    )
                )

        return RecallApplyResult(recall=recall, sync_events=sync_events, updated=True)

    def collect_due_burn_sync_events(self, limit: int) -> list[StoredSyncEvent]:
        if not self.m_burn_enabled:
            return []
        now_ms = self._now_ms()
        safe_limit = max(1, min(int(limit), 500))
        connection = self.m_db.m_connection
        sync_events: list[StoredSyncEvent] = []
        affected_message_ids: set[str] = set()

        with connection:
            rows = connection.execute(
                """
                SELECT
                  d.server_msg_id,
                  d.user_id,
                  m.conversation_id
                FROM message_deliveries AS d
                JOIN messages AS m ON m.server_msg_id = d.server_msg_id
                WHERE m.recalled = 0
                  AND m.burn_mode = ?
                  AND d.burn_at_ms IS NOT NULL
                  AND d.burn_at_ms > 0
                  AND d.burn_at_ms <= ?
                  AND d.burned_at_ms IS NULL
                ORDER BY d.burn_at_ms ASC, d.server_msg_id ASC, d.user_id ASC
                LIMIT ?
                """,
                (self.BURN_MODE_AFTER_READ, now_ms, safe_limit),
            ).fetchall()

            for row in rows:
                message_id = str(row["server_msg_id"])
                target_user_id = str(row["user_id"])
                conversation_id = str(row["conversation_id"])
                updated_rows = connection.execute(
                    """
                    UPDATE message_deliveries
                    SET burned_at_ms = ?
                    WHERE server_msg_id = ?
                      AND user_id = ?
                      AND burned_at_ms IS NULL
                      AND burn_at_ms IS NOT NULL
                      AND burn_at_ms <= ?
                    """,
                    (now_ms, message_id, target_user_id, now_ms),
                ).rowcount
                if updated_rows <= 0:
                    continue

                payload_event_id = str(uuid.uuid4())
                recall = message_pb2.Recall(
                    event_id=payload_event_id,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    ts_ms=now_ms,
                    operator_id="system-burn",
                )
                payload = recall.SerializeToString()
                event_id = str(uuid.uuid4())
                global_seq = self._next_global_seq(connection, target_user_id)
                connection.execute(
                    """
                    INSERT INTO sync_events(event_id, user_id, seq, conversation_id, event_type, payload, created_at_ms)
                    VALUES(?, ?, ?, ?, 'recall', ?, ?)
                    """,
                    (event_id, target_user_id, global_seq, conversation_id, payload, now_ms),
                )
                sync_events.append(
                    StoredSyncEvent(
                        user_id=target_user_id,
                        global_seq=global_seq,
                        event_id=payload_event_id,
                        event_type="recall",
                        payload=payload,
                    )
                )
                affected_message_ids.add(message_id)

            for message_id in affected_message_ids:
                connection.execute(
                    """
                    UPDATE messages
                    SET recalled = 1
                    WHERE server_msg_id = ?
                      AND recalled = 0
                      AND NOT EXISTS(
                        SELECT 1
                        FROM message_deliveries
                        WHERE server_msg_id = ?
                          AND burned_at_ms IS NULL
                      )
                    """,
                    (message_id, message_id),
                )

        return sync_events

    def purge_burned_message_content(self, limit: int) -> int:
        if not self.m_burn_enabled:
            return 0
        safe_limit = max(1, min(int(limit), 500))
        connection = self.m_db.m_connection
        purged = 0

        with connection:
            rows = connection.execute(
                """
                SELECT m.server_msg_id, m.type, m.content
                FROM messages AS m
                WHERE m.burn_mode = ?
                  AND m.recalled = 1
                  AND LENGTH(m.content) > 0
                  AND NOT EXISTS(
                    SELECT 1
                    FROM message_deliveries AS d
                    WHERE d.server_msg_id = m.server_msg_id
                      AND d.burned_at_ms IS NULL
                  )
                ORDER BY m.created_at_ms ASC, m.server_msg_id ASC
                LIMIT ?
                """,
                (self.BURN_MODE_AFTER_READ, safe_limit),
            ).fetchall()

            for row in rows:
                message_id = str(row["server_msg_id"])
                message_type = int(row["type"])
                content = row["content"]
                if isinstance(content, memoryview):
                    content = content.tobytes()
                replacement = self._build_purged_content(message_type, bytes(content))
                affected = connection.execute(
                    """
                    UPDATE messages
                    SET content = ?
                    WHERE server_msg_id = ?
                      AND LENGTH(content) > 0
                    """,
                    (replacement, message_id),
                ).rowcount
                if affected > 0:
                    purged += affected

        return purged

    @staticmethod
    def _build_purged_content(message_type: int, content: bytes) -> bytes:
        if int(message_type) != int(common_pb2.MSG_FILE):
            return b""
        file_id = ""
        try:
            parsed = json.loads(content.decode("utf-8"))
            if isinstance(parsed, dict):
                file_id = str(parsed.get("fileId", "")).strip()
        except Exception:
            file_id = ""
        if not file_id:
            return b""
        minimal_payload = {
            "kind": "file",
            "fileId": file_id,
        }
        return json.dumps(minimal_payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
