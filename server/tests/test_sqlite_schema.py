import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage.sqlite.db import MiniImSqliteDb


class SqliteSchemaTest(unittest.TestCase):
    def test_init_schema_creates_core_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'test.db'
            db = MiniImSqliteDb(db_path)
            db.init_schema()

            rows = db.m_connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('users','messages','sync_cursors')"
            ).fetchall()
            names = {row['name'] for row in rows}
            self.assertIn('users', names)
            self.assertIn('messages', names)
            self.assertIn('sync_cursors', names)
            db.close()


if __name__ == '__main__':
    unittest.main()
