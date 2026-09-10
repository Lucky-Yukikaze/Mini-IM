from pathlib import Path

from storage.sqlite.db import MiniImSqliteDb


def _ensure_file_transfer_columns(db: MiniImSqliteDb) -> None:
    rows = db.execute_fetchall("PRAGMA table_info(file_transfers)")
    if not rows:
        return
    columns = {str(row["name"]) for row in rows}
    if "version" not in columns:
        db.execute_write("ALTER TABLE file_transfers ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
    if "source_file_id" not in columns:
        db.execute_write("ALTER TABLE file_transfers ADD COLUMN source_file_id TEXT NOT NULL DEFAULT ''")
        # Legacy download sources were not stored. Preserve the rows without guessing a source from a digest.


def _ensure_messages_burn_columns(db: MiniImSqliteDb) -> None:
    rows = db.execute_fetchall("PRAGMA table_info(messages)")
    if not rows:
        return
    columns = {str(row["name"]) for row in rows}
    if "content_purged_at_ms" not in columns:
        db.execute_write("ALTER TABLE messages ADD COLUMN content_purged_at_ms INTEGER NOT NULL DEFAULT 0")
    if "burn_mode" not in columns:
        db.execute_write("ALTER TABLE messages ADD COLUMN burn_mode INTEGER NOT NULL DEFAULT 0")
    if "burn_ttl_sec" not in columns:
        db.execute_write("ALTER TABLE messages ADD COLUMN burn_ttl_sec INTEGER NOT NULL DEFAULT 0")


def _ensure_message_deliveries_burn_columns(db: MiniImSqliteDb) -> None:
    rows = db.execute_fetchall("PRAGMA table_info(message_deliveries)")
    if not rows:
        return
    columns = {str(row["name"]) for row in rows}
    if "burn_started_at_ms" not in columns:
        db.execute_write("ALTER TABLE message_deliveries ADD COLUMN burn_started_at_ms INTEGER")
    if "burn_at_ms" not in columns:
        db.execute_write("ALTER TABLE message_deliveries ADD COLUMN burn_at_ms INTEGER")
    if "burned_at_ms" not in columns:
        db.execute_write("ALTER TABLE message_deliveries ADD COLUMN burned_at_ms INTEGER")
    db.execute_write(
        "CREATE INDEX IF NOT EXISTS idx_message_deliveries_burn_due ON message_deliveries(burn_at_ms, burned_at_ms)"
    )


def _ensure_delivery_confirmation_columns(db: MiniImSqliteDb) -> None:
    rows = db.execute_fetchall("PRAGMA table_info(message_deliveries)")
    if not rows or "sent_at_ms" in {str(row["name"]) for row in rows}:
        return
    # The old delivered timestamp was written at send time, even for offline users.
    # Add the migration marker and repair its data in the same transaction.
    with db.transaction() as connection:
        connection.execute("ALTER TABLE message_deliveries ADD COLUMN sent_at_ms INTEGER NOT NULL DEFAULT 0")
        connection.execute("""
            UPDATE message_deliveries SET
              sent_at_ms = COALESCE((SELECT created_at_ms FROM messages
                                    WHERE server_msg_id = message_deliveries.server_msg_id), 0),
              delivered_at_ms = CASE WHEN status IN ('read', 'delivered') THEN read_at_ms
                                     ELSE delivered_at_ms END,
              status = CASE WHEN status = 'delivered' THEN
                              CASE WHEN read_at_ms IS NULL THEN 'sent' ELSE 'read' END
                            ELSE status END
        """)


def _ensure_sync_event_columns(db: MiniImSqliteDb) -> None:
    rows = db.execute_fetchall("PRAGMA table_info(sync_events)")
    if rows and "entity_id" not in {str(row["name"]) for row in rows}:
        db.execute_write("ALTER TABLE sync_events ADD COLUMN entity_id TEXT NOT NULL DEFAULT ''")


def init_db(db_path: Path) -> None:
    from storage.repo.sync_event import MigrateMessageEvents
    from storage.repo.message_intent import MigrateMessageIntents
    from storage.repo.read_state import MigrateMembershipReads, MigrateReadCountEvents

    db = MiniImSqliteDb(db_path)
    try:
        _ensure_file_transfer_columns(db)
        _ensure_messages_burn_columns(db)
        _ensure_message_deliveries_burn_columns(db)
        _ensure_sync_event_columns(db)
        _ensure_delivery_confirmation_columns(db)
        db.init_schema()
        with db.transaction() as connection:
            MigrateMessageIntents(connection)
            MigrateMessageEvents(connection)
            MigrateMembershipReads(connection)
            MigrateReadCountEvents(connection)
    finally:
        db.close()
