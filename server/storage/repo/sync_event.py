"""Create the same per-user event identity for storage and live delivery."""

from dataclasses import dataclass
import json
import uuid

from google.protobuf.message import Message
from protocol.pb import common_pb2, message_pb2


@dataclass
class StoredSyncEvent:
    user_id: str
    global_seq: int
    event_id: str
    event_type: str
    payload: bytes


def AppendSyncEvents(connection, user_ids, conversation_id: str, event_type: str,
                     body: Message, now_ms: int) -> list[StoredSyncEvent]:
    events = []
    for user_id in dict.fromkeys(user_ids):
        event_id = str(uuid.uuid4())
        row = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM sync_events WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        global_seq = int(row["next_seq"])
        user_body = type(body)()
        user_body.CopyFrom(body)
        if "event_id" in user_body.DESCRIPTOR.fields_by_name:
            user_body.event_id = event_id
        payload = user_body.SerializeToString()
        entity_id = user_body.message_id if event_type == "message" else ""
        connection.execute(
            """
            INSERT INTO sync_events(event_id, user_id, seq, conversation_id, event_type, entity_id, payload, created_at_ms)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, user_id, global_seq, conversation_id, event_type, entity_id, payload, now_ms),
        )
        events.append(StoredSyncEvent(user_id, global_seq, event_id, event_type, payload))
    return events


def PurgedMessageContent(message_type: int, content: bytes) -> bytes:
    if int(message_type) != int(common_pb2.MSG_FILE):
        return b""
    try:
        parsed = json.loads(content.decode("utf-8"))
        file_id = str(parsed.get("fileId", "")).strip() if isinstance(parsed, dict) else ""
    except (ValueError, UnicodeDecodeError):
        file_id = ""
    if not file_id:
        return b""
    return json.dumps({"kind": "file", "fileId": file_id}, separators=(",", ":")).encode("utf-8")


def RedactEventRows(connection, rows) -> None:
    for row in rows:
        message = message_pb2.Message.FromString(bytes(row["payload"]))
        message.content = PurgedMessageContent(message.type, message.content)
        message.recalled = True
        payload = message.SerializeToString()
        if payload != bytes(row["payload"]):
            connection.execute(
                "UPDATE sync_events SET payload = ? WHERE event_id = ?", (payload, row["event_id"]),
            )


def RedactMessageEvents(connection, message_id: str, user_id: str | None = None) -> None:
    params = [message_id]
    user_filter = ""
    if user_id is not None:
        user_filter = " AND user_id = ?"
        params.append(user_id)
    rows = connection.execute(
        "SELECT event_id, payload FROM sync_events WHERE event_type = 'message' AND entity_id = ?" + user_filter,
        params,
    ).fetchall()
    RedactEventRows(connection, rows)


def MigrateMessageEvents(connection) -> None:
    rows = connection.execute(
        "SELECT event_id, payload FROM sync_events WHERE event_type = 'message' AND entity_id = ''",
    ).fetchall()
    for row in rows:
        message = message_pb2.Message.FromString(bytes(row["payload"]))
        connection.execute(
            "UPDATE sync_events SET entity_id = ? WHERE event_id = ?", (message.message_id, row["event_id"]),
        )
    unavailable = connection.execute(
        """
        SELECT e.event_id, e.payload FROM sync_events AS e
        JOIN messages AS m ON m.server_msg_id = e.entity_id
        LEFT JOIN message_deliveries AS d ON d.server_msg_id = e.entity_id AND d.user_id = e.user_id
        WHERE e.event_type = 'message' AND (m.recalled = 1 OR d.burned_at_ms IS NOT NULL)
        """
    ).fetchall()
    RedactEventRows(connection, unavailable)
