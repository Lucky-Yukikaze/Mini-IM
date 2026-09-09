from __future__ import annotations

from dataclasses import dataclass
import time

from protocol.pb import message_pb2, sync_pb2
from storage.repo.control_write_repo import ControlWriteRepo, ControlWriteResult
from storage.repo.sync_event import AppendSyncEvents

from storage.sqlite.db import MiniImSqliteDb


@dataclass
class SyncEventRow:
    event_id: str
    seq: int
    event_type: str
    payload: bytes


class SyncRepo:
    def __init__(self, db: MiniImSqliteDb) -> None:
        self.m_db = db

    def load_events(
        self,
        user_id: str,
        after_global_cursor: int,
        limit: int,
        conversation_id: str = "",
    ) -> list[SyncEventRow]:
        params: list[object] = [user_id, int(after_global_cursor)]
        filter_clause = ""
        if conversation_id:
            filter_clause = "AND conversation_id = ?"
            params.append(conversation_id)

        rows = self.m_db.execute_fetchall(
            f"""
            SELECT event_id, seq, event_type, payload
            FROM sync_events
            WHERE user_id = ? AND seq > ? {filter_clause}
            ORDER BY seq ASC
            LIMIT ?
            """,
            tuple([*params, int(limit)]),
        )

        result: list[SyncEventRow] = []
        for row in rows:
            payload = row["payload"]
            if isinstance(payload, memoryview):
                payload = payload.tobytes()
            result.append(
                SyncEventRow(
                    event_id=row["event_id"],
                    seq=int(row["seq"]),
                    event_type=row["event_type"],
                    payload=bytes(payload),
                )
            )
        return result

    def confirm_applied(self, user_id: str, device_id: str, request_id: str, cursor: int) -> ControlWriteResult:
        if not device_id or cursor < 0:
            return ControlWriteRepo.reject(request_id, 400, "confirmation requires device and nonnegative cursor")
        now_ms = int(time.time() * 1000)
        with self.m_db.transaction() as connection:
            maximum = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM sync_events WHERE user_id=?", (user_id,)).fetchone()[0]
            if cursor > maximum:
                return ControlWriteRepo.reject(request_id, 400, "confirmation exceeds user sync stream")
            previous = connection.execute(
                "SELECT global_cursor FROM sync_applied_cursors WHERE user_id=? AND device_id=?",
                (user_id, device_id)).fetchone()
            old_cursor = int(previous[0]) if previous else 0
            events = []
            if cursor > old_cursor:
                rows = connection.execute("""
                    SELECT d.*, m.sender_id FROM message_deliveries AS d
                    JOIN messages AS m ON m.server_msg_id=d.server_msg_id
                    WHERE d.user_id=? AND d.status='sent' AND d.server_msg_id IN (
                      SELECT entity_id FROM sync_events
                      WHERE user_id=? AND event_type='message' AND seq>? AND seq<=?
                    ) ORDER BY d.conversation_id, d.seq
                """, (user_id, user_id, old_cursor, cursor)).fetchall()
                for row in rows:
                    delivered_at = max(now_ms, int(row["sent_at_ms"]))
                    connection.execute("""
                        UPDATE message_deliveries SET status='delivered', delivered_at_ms=?
                        WHERE server_msg_id=? AND user_id=? AND status='sent'
                    """, (delivered_at, row["server_msg_id"], user_id))
                    body = sync_pb2.DeliveryUpdated(
                        conversation_id=row["conversation_id"], message_id=row["server_msg_id"],
                        user_id=user_id, status="delivered", sent_at_ms=row["sent_at_ms"],
                        delivered_at_ms=delivered_at)
                    events.extend(AppendSyncEvents(connection, [row["sender_id"], user_id],
                        row["conversation_id"], "delivery_updated", body, now_ms))
                connection.execute("""
                    INSERT INTO sync_applied_cursors(user_id, device_id, global_cursor, updated_at_ms)
                    VALUES(?,?,?,?) ON CONFLICT(user_id,device_id) DO UPDATE SET
                    global_cursor=excluded.global_cursor, updated_at_ms=excluded.updated_at_ms
                """, (user_id, device_id, cursor, now_ms))
            ack = message_pb2.Ack(request_id=request_id, success=True, code=0,
                                 entity_id=str(cursor), server_time_ms=now_ms)
            return ControlWriteResult(ack, events)
