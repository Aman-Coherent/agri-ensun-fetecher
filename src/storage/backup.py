from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.config_loader import DATA_DIR, country_slug, ensure_dirs, settings
from src.storage.sqlite_durability import configure_connection, flush_to_disk, integrity_check

BACKUP_ROOT = DATA_DIR / "backups"


def backup_dir(continent_code: str, country_name: str) -> Path:
    slug = country_slug(country_name)
    return BACKUP_ROOT / continent_code / slug


def list_backups(continent_code: str, country_name: str) -> list[Path]:
    directory = backup_dir(continent_code, country_name)
    if not directory.exists():
        return []
    slug = country_slug(country_name)
    files = sorted(directory.glob(f"{slug}_*.db"), reverse=True)
    return files


def latest_backup(continent_code: str, country_name: str) -> Path | None:
    backups = list_backups(continent_code, country_name)
    return backups[0] if backups else None


def latest_backup_mtime(continent_code: str, country_name: str) -> datetime | None:
    path = latest_backup(continent_code, country_name)
    if path is None:
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def rotate_backups(directory: Path, slug: str) -> None:
    keep = int(settings().get("backup_keep_count", 5))
    files = sorted(directory.glob(f"{slug}_*.db"), reverse=True)
    for old in files[keep:]:
        old.unlink(missing_ok=True)


def create_backup(conn: sqlite3.Connection, continent_code: str, country_name: str) -> Path:
    slug = country_slug(country_name)
    directory = backup_dir(continent_code, country_name)
    ensure_dirs(directory)
    flush_to_disk(conn)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    dest = directory / f"{slug}_{timestamp}.db"
    dest_conn = sqlite3.connect(dest)
    try:
        configure_connection(dest_conn)
        conn.backup(dest_conn)
        flush_to_disk(dest_conn)
    finally:
        dest_conn.close()
    rotate_backups(directory, slug)
    return dest


def restore_from_backup(backup_path: Path, target_path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(target_path) + suffix)
        sidecar.unlink(missing_ok=True)
    shutil.copy2(backup_path, target_path)


def try_auto_restore(continent_code: str, country_name: str, target_path: Path) -> Path | None:
    backup_path = latest_backup(continent_code, country_name)
    if backup_path is None:
        return None
    restore_from_backup(backup_path, target_path)
    return backup_path


def verify_database(conn: sqlite3.Connection, db_path: Path) -> dict[str, str | int | float | None]:
    issue = integrity_check(conn)
    size_mb = db_path.stat().st_size / (1024 * 1024) if db_path.exists() else 0.0
    return {
        "path": str(db_path.resolve()),
        "integrity": issue or "ok",
        "size_mb": round(size_mb, 2),
    }
