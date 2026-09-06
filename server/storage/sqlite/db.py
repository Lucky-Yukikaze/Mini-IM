from __future__ import annotations

from pathlib import Path
import sqlite3


class MiniImSqliteDb:
    def __init__(self, db_path: Path) -> None:
        self.m_db_path = db_path
        self.m_connection = sqlite3.connect(str(db_path), check_same_thread=False)
        self.m_connection.row_factory = sqlite3.Row

    def init_schema(self) -> None:
        schema_path = Path(__file__).resolve().parent / "schema.sql"
        schema_sql = schema_path.read_text(encoding="utf-8")
        self.m_connection.executescript(schema_sql)
        self.m_connection.commit()

    def execute_write(self, sql: str, params: tuple[object, ...] = ()) -> None:
        self.m_connection.execute(sql, params)
        self.m_connection.commit()

    def execute_fetchone(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Row | None:
        cursor = self.m_connection.execute(sql, params)
        return cursor.fetchone()

    def execute_fetchall(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        cursor = self.m_connection.execute(sql, params)
        return cursor.fetchall()

    def close(self) -> None:
        self.m_connection.close()
