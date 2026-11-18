from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config_loader import DATA_DIR, ensure_dirs, settings
from src.storage.file_lock import FileLock, FileLockTimeout
from src.storage.sqlite_durability import configure_connection, flush_to_disk


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionStore:
    def __init__(self, path: Path | None = None) -> None:
        ensure_dirs(DATA_DIR)
        self.path = path or DATA_DIR / "sessions.db"
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        configure_connection(self.conn)
        self._init_schema()

    def _cookie_lock(self) -> FileLock:
        timeout = float(settings().get("session_lock_timeout_seconds", 120))
        return FileLock(self.lock_path, timeout=timeout)

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cookies (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cookie_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def save_cookies(self, cookies: list[dict[str, Any]]) -> None:
        try:
            with self._cookie_lock():
                self.conn.execute(
                    """
                    INSERT INTO cookies (id, cookie_json, updated_at)
                    VALUES (1, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        cookie_json = excluded.cookie_json,
                        updated_at = excluded.updated_at
                    """,
                    (json.dumps(cookies), _utc_now()),
                )
                flush_to_disk(self.conn)
        except FileLockTimeout as exc:
            raise RuntimeError(
                "Another scraper is updating shared cookies (sessions.db). "
                "Wait and retry, or run one country at a time during cookie refresh."
            ) from exc

    def get_cookie_updated_at(self) -> str | None:
        try:
            row = self.conn.execute(
                "SELECT updated_at FROM cookies WHERE id = 1"
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if not row:
            return None
        return str(row[0]) if row[0] is not None else None

    def load_cookies(self) -> list[dict[str, Any]] | None:
        try:
            with self._cookie_lock():
                row = self.conn.execute(
                    "SELECT cookie_json FROM cookies WHERE id = 1"
                ).fetchone()
        except FileLockTimeout:
            row = self.conn.execute(
                "SELECT cookie_json FROM cookies WHERE id = 1"
            ).fetchone()
        if not row:
            return None
        return json.loads(row["cookie_json"])

    def close(self) -> None:
        flush_to_disk(self.conn)
        self.conn.close()
