"""Rebuild display counts from original deliveries and preserve departed readers."""
import time

from protocol.pb import message_pb2


def RebuildReadCounters(connection, now_ms, conversation_id="", after_seq=0, through_seq=9223372036854775807):
    connection.execute("""
        INSERT INTO message_read_counters(server_msg_id,conversation_id,conversation_seq,
                                          member_count,read_count,unread_count,updated_at_ms)
        SELECT m.server_msg_id,m.conversation_id,m.conversation_seq,COUNT(d.user_id),
               SUM(CASE WHEN d.read_at_ms IS NOT NULL THEN 1 ELSE 0 END),
               COUNT(d.user_id)-SUM(CASE WHEN d.read_at_ms IS NOT NULL THEN 1 ELSE 0 END),?
        FROM messages AS m
        LEFT JOIN message_deliveries AS d ON d.server_msg_id=m.server_msg_id AND d.user_id<>m.sender_id
        WHERE (?='' OR m.conversation_id=?) AND m.conversation_seq>? AND m.conversation_seq<=?
        GROUP BY m.server_msg_id
        ON CONFLICT(server_msg_id) DO UPDATE SET member_count=excluded.member_count,
            read_count=excluded.read_count,unread_count=excluded.unread_count,updated_at_ms=excluded.updated_at_ms
    """, (now_ms, conversation_id, conversation_id, after_seq, through_seq))


def PreserveReadPosition(connection, conversation_id, user_id, last_read_seq, now_ms):
    connection.execute("""
        INSERT INTO conversation_read_history(conversation_id,user_id,last_read_seq,updated_at_ms)
        VALUES(?,?,?,?) ON CONFLICT(conversation_id,user_id) DO UPDATE SET
        last_read_seq=MAX(last_read_seq,excluded.last_read_seq),updated_at_ms=excluded.updated_at_ms
    """, (conversation_id, user_id, last_read_seq, now_ms))


def MigrateMembershipReads(connection):
    name = "membership_read_history_v1"
    if connection.execute("SELECT 1 FROM schema_migrations WHERE name=?", (name,)).fetchone():
        return
    now_ms = int(time.time() * 1000)
    for row in connection.execute("SELECT conversation_id,user_id,last_read_seq FROM conversation_members").fetchall():
        PreserveReadPosition(connection, row["conversation_id"], row["user_id"], row["last_read_seq"], now_ms)
    for row in connection.execute("SELECT user_id,conversation_id,payload FROM sync_events WHERE event_type='receipt'"):
        receipt = message_pb2.Receipt.FromString(bytes(row["payload"]))
        if row["user_id"] == receipt.reader_id and row["conversation_id"] == receipt.conversation_id:
            PreserveReadPosition(connection, receipt.conversation_id, receipt.reader_id, receipt.last_read_seq, now_ms)
    connection.execute("""
        UPDATE conversation_members SET last_read_seq=MAX(last_read_seq,COALESCE((
            SELECT h.last_read_seq FROM conversation_read_history AS h
            WHERE h.conversation_id=conversation_members.conversation_id AND h.user_id=conversation_members.user_id
        ),0))
    """)
    RebuildReadCounters(connection, now_ms)
    connection.execute("INSERT INTO schema_migrations(name,applied_at_ms) VALUES(?,?)", (name, now_ms))
