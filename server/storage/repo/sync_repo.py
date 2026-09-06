from __future__ import annotations

from dataclasses import dataclass

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
