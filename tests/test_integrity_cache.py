"""Integrity check cache on country database open."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.storage.country_db import CountryDatabase
from src.storage.sqlite_durability import configure_connection


class TestIntegrityCache(unittest.TestCase):
    def test_store_and_read_integrity_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "probe.db"
            conn = sqlite3.connect(db_path)
            configure_connection(conn)
            conn.execute(
                """
                CREATE TABLE run_meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    continent TEXT NOT NULL,
                    country TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    integrity_db_size INTEGER,
                    integrity_db_mtime_ns INTEGER,
                    integrity_checked_at TEXT
                )
                """
            )
            conn.commit()
            CountryDatabase._store_integrity_cache(conn, 12345, 67890)
            cached = CountryDatabase._read_integrity_cache(conn)
            self.assertEqual(cached, (12345, 67890))
            conn.close()

    def test_read_integrity_cache_missing_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "probe.db"
            conn = sqlite3.connect(db_path)
            configure_connection(conn)
            conn.execute(
                """
                CREATE TABLE run_meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    continent TEXT NOT NULL,
                    country TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
            self.assertIsNone(CountryDatabase._read_integrity_cache(conn))
            conn.close()


if __name__ == "__main__":
    unittest.main()
