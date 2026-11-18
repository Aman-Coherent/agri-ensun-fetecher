from __future__ import annotations

import sqlite3

from src.config_loader import settings


def configure_connection(conn: sqlite3.Connection) -> None:
    cfg = settings()
    sync_mode = str(cfg.get("sqlite_synchronous", "FULL")).upper()
    wal_pages = int(cfg.get("wal_autocheckpoint", 50))
    busy_ms = int(cfg.get("sqlite_busy_timeout_ms", 30000))
    # busy_timeout MUST be set first: it controls how long SQLite will wait/retry
    # if the file is momentarily locked by another connection. If journal_mode is
    # set first (with the default 0ms busy timeout), a locked file fails instantly
    # instead of retrying.
    conn.execute(f"PRAGMA busy_timeout={busy_ms}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA synchronous={sync_mode}")
    conn.execute(f"PRAGMA wal_autocheckpoint={wal_pages}")


def configure_readonly_connection(conn: sqlite3.Connection) -> None:
    """Configure a connection opened strictly for reading (e.g. CSV export).

    Only sets busy_timeout (a per-connection runtime setting, safe on a
    read-only handle). Deliberately never touches journal_mode/synchronous/
    wal_autocheckpoint since those can require write access to the database
    file header and are unnecessary for reads.
    """
    cfg = settings()
    busy_ms = int(cfg.get("sqlite_busy_timeout_ms", 30000))
    conn.execute(f"PRAGMA busy_timeout={busy_ms}")


def flush_to_disk(conn: sqlite3.Connection) -> None:
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def flush_passive(conn: sqlite3.Connection) -> None:
    """Commit and passive WAL checkpoint — does not block other writers."""
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")


def integrity_check(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    if row is None:
        return "integrity_check returned no result"
    result = row[0] if isinstance(row, tuple) else row["integrity_check"]
    if result != "ok":
        return str(result)
    return None
