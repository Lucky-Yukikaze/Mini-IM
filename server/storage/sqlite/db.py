from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
import sqlite3
import uuid


class MiniImSqliteDb:
    def __init__(self, db_path: Path) -> None:
        self.m_db_path = db_path
        self.m_connection = sqlite3.connect(str(db_path), check_same_thread=False)
        self.m_connection.row_factory = sqlite3.Row
        self.m_connection.execute("PRAGMA journal_mode = WAL")
        self.m_connection.execute("PRAGMA synchronous = NORMAL")
        self.m_connection.execute("PRAGMA foreign_keys = ON")

    def init_schema(self) -> None:
        schema_path = Path(__file__).resolve().parent / "schema.sql"
        schema_sql = schema_path.read_text(encoding="utf-8")
        self.m_connection.executescript(schema_sql)
        self.m_connection.commit()

    def execute_write(self, sql: str, params: tuple[object, ...] = ()) -> None:
        with self.transaction() as connection:
            connection.execute(sql, params)

    @contextmanager
    def transaction(self):
        """Keep nested repository operations inside their caller's transaction."""
        connection = self.m_connection
        savepoint = "miniim_" + uuid.uuid4().hex
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            yield connection
        except BaseException:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")

    def execute_fetchone(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Row | None:
        cursor = self.m_connection.execute(sql, params)
        return cursor.fetchone()

    def execute_fetchall(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        cursor = self.m_connection.execute(sql, params)
        return cursor.fetchall()

    def close(self) -> None:
        self.m_connection.close()
