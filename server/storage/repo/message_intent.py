"""Persist message intent identity without retaining an extra plaintext copy."""
import hashlib

from protocol.pb import common_pb2, message_pb2


def MessageIntentFingerprint(request: message_pb2.SendMessage) -> bytes:
    # Only business fields define identity; unknown protobuf fields are not interpreted.
    message_type = request.type or common_pb2.MSG_TEXT
    burn_mode = 0 if message_type == common_pb2.MSG_SYSTEM else request.burn_mode
    canonical = message_pb2.SendMessage(
        conversation_id=request.conversation_id, client_msg_id=request.client_msg_id,
        type=message_type, content=request.content, burn_mode=burn_mode,
        burn_ttl_sec=request.burn_ttl_sec if burn_mode else 0)
    return hashlib.sha256(canonical.SerializeToString(deterministic=True)).digest()


def MigrateMessageIntents(connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(messages)")}
    if "intent_fingerprint" in columns:
        return
    # Column and backfill commit together. Already purged content cannot prove original identity.
    connection.execute("ALTER TABLE messages ADD COLUMN intent_fingerprint BLOB")
    for row in connection.execute("SELECT * FROM messages WHERE content_purged_at_ms=0"):
        request = message_pb2.SendMessage(conversation_id=row["conversation_id"], client_msg_id=row["client_msg_id"],
            type=row["type"], content=bytes(row["content"]), burn_mode=row["burn_mode"], burn_ttl_sec=row["burn_ttl_sec"])
        connection.execute("UPDATE messages SET intent_fingerprint=? WHERE server_msg_id=?",
                           (MessageIntentFingerprint(request), row["server_msg_id"]))
