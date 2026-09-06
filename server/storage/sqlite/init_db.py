from pathlib import Path

from storage.sqlite.db import MiniImSqliteDb


def _ensure_file_transfer_columns(db: MiniImSqliteDb) -> None:
    rows = db.execute_fetchall("PRAGMA table_info(file_transfers)")
    if not rows:
        return
    columns = {str(row["name"]) for row in rows}
    if "version" not in columns:
        db.execute_write("ALTER TABLE file_transfers ADD COLUMN version INTEGER NOT NULL DEFAULT 1")


def _ensure_messages_burn_columns(db: MiniImSqliteDb) -> None:
    rows = db.execute_fetchall("PRAGMA table_info(messages)")
    if not rows:
        return
    columns = {str(row["name"]) for row in rows}
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


def init_db(db_path: Path) -> None:
    db = MiniImSqliteDb(db_path)
    _ensure_file_transfer_columns(db)
    _ensure_messages_burn_columns(db)
    _ensure_message_deliveries_burn_columns(db)
    db.init_schema()
    _ensure_file_transfer_columns(db)
    _ensure_messages_burn_columns(db)
    _ensure_message_deliveries_burn_columns(db)
    db.close()
