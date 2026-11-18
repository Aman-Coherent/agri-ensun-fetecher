from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Callable

from src.config_loader import settings
from src.storage.backup import create_backup, latest_backup_mtime
from src.storage.country_db import CountryDatabase
from src.storage.exports import export_country_csv, export_profile_csv
from src.storage.progress_ledger import append_entry, count_entries


class RunDurability:
    """Coordinates flush, backup, ledger, and periodic CSV export during a scrape run."""

    def __init__(
        self,
        db: CountryDatabase,
        log: Callable[[str], None],
        *,
        mode: str = "search",
    ) -> None:
        self.db = db
        self._log = log
        self.mode = mode
        cfg = settings()
        if mode == "profile":
            self.backup_every = int(cfg.get("profile_backup_every", 100))
            self.export_every = int(cfg.get("profile_export_every", 100))
            self.flush_every = int(cfg.get("profile_flush_every", 10))
            self.export_on_run_end_only = bool(
                cfg.get("profile_export_on_run_end_only", False)
            )
        else:
            self.backup_every = int(cfg.get("backup_every_n_searches", 50))
            self.export_every = int(cfg.get("export_csv_every_n_searches", 100))
            self.flush_every = 1
            self.export_on_run_end_only = False
        self._since_backup = 0
        self._since_export = 0
        self._since_flush = 0
        self._heavy_ops_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Search phase
    # ------------------------------------------------------------------

    def on_search_node_finished(
        self,
        *,
        node_id: str,
        status: str,
        search_term: str,
        company_type: str = "",
        industry: str = "",
        depth: int = 0,
        location_triple: str = "",
        result_count: int = 0,
        pages_fetched: int = 0,
        facet_total: int | None = None,
        error_message: str | None = None,
    ) -> None:
        """Called after every live ensun search (success or failure)."""
        self.db.flush_to_disk()
        append_entry(
            self.db.continent_code,
            self.db.country_name,
            {
                "kind": "search",
                "node_id": node_id,
                "status": status,
                "search_term": search_term,
                "company_type": company_type,
                "industry": industry,
                "depth": depth,
                "location_triple": location_triple,
                "result_count": result_count,
                "pages_fetched": pages_fetched,
                "facet_total": facet_total,
                "error_message": error_message,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        self._maybe_backup()
        self._maybe_export_search()

    # ------------------------------------------------------------------
    # Profile phase (unchanged behaviour from the nutraceutical scraper)
    # ------------------------------------------------------------------

    def append_profile_ledger(
        self, company_id: str, status: str, company_name: str = ""
    ) -> None:
        """Fast path: ledger only (call under short lock or from thread pool)."""
        append_entry(
            self.db.continent_code,
            self.db.country_name,
            {
                "kind": "profile",
                "company_id": company_id,
                "status": status,
                "company_name": company_name or company_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        self._since_flush += 1
        self._since_backup += 1
        self._since_export += 1

    def run_profile_heavy_ops(self) -> None:
        """Flush/backup/export — run outside worker scrape path (thread pool)."""
        with self._heavy_ops_lock:
            if self._since_flush >= self.flush_every:
                self.db.flush_profile_checkpoint()
                self._since_flush = 0
            if self._since_backup >= self.backup_every:
                path = create_backup(
                    self.db.conn, self.db.continent_code, self.db.country_name
                )
                self._log(f"Backup saved: {path}")
                self._since_backup = 0
            if not self.export_on_run_end_only and self._since_export >= self.export_every:
                self._safe_export(export_profile_csv, "Periodic export")
                self._since_export = 0

    def on_profile_finished(self, company_id: str, status: str, company_name: str = "") -> None:
        self.append_profile_ledger(company_id, status, company_name)
        self.run_profile_heavy_ops()

    # ------------------------------------------------------------------
    # Shared
    # ------------------------------------------------------------------

    def on_run_end(self) -> None:
        if self.mode == "profile":
            self.db.flush_profile_checkpoint_full()
        else:
            self.db.flush_to_disk()
        try:
            path = create_backup(self.db.conn, self.db.continent_code, self.db.country_name)
            self._log(f"Final backup: {path}")
        except Exception as exc:
            self._log(f"Final backup failed: {exc}")
        if self.mode == "search":
            self._safe_export(export_country_csv, "Exported")
        else:
            self._safe_export(export_profile_csv, "Exported")

    def _safe_export(self, export_fn: Callable[[CountryDatabase], list[Any]], label: str) -> None:
        """Exports must never break a scrape (e.g. CSV open in Excel = locked)."""
        try:
            for path in export_fn(self.db):
                self._log(f"{label}: {path}")
        except PermissionError as exc:
            self._log(
                f"{label} skipped — file is locked (close it in Excel?): {exc}. "
                "Data is safe in the database; run `python -m src.cli export` later."
            )
        except Exception as exc:
            self._log(f"{label} failed: {exc} (data is safe in the database)")

    def _maybe_backup(self) -> None:
        self._since_backup += 1
        if self._since_backup < self.backup_every:
            return
        try:
            path = create_backup(self.db.conn, self.db.continent_code, self.db.country_name)
            self._log(f"Backup saved: {path}")
        except Exception as exc:
            self._log(f"Backup failed: {exc}")
        self._since_backup = 0

    def _maybe_export_search(self) -> None:
        self._since_export += 1
        if self._since_export < self.export_every:
            return
        self._safe_export(export_country_csv, "Periodic export")
        self._since_export = 0

    def _maybe_export_profile(self) -> None:
        self._since_export += 1
        if self._since_export < self.export_every:
            return
        self._safe_export(export_profile_csv, "Periodic export")
        self._since_export = 0


def format_startup_banner(db: CountryDatabase) -> str:
    stats = db.search_stats()
    size_mb = db.path.stat().st_size / (1024 * 1024) if db.path.exists() else 0.0
    last_backup = latest_backup_mtime(db.continent_code, db.country_name)
    ledger_count = count_entries(db.continent_code, db.country_name)
    backup_label = last_backup.strftime("%Y-%m-%d %H:%M") if last_backup else "none yet"
    failed = stats.get("failed", 0)
    retry = f" ({failed:,} failed - retried this run)" if failed else ""
    return (
        f"Database    : {db.path.resolve()} ({size_mb:.1f} MB)\n"
        f"Saved so far: {stats['terms_done']:,} terms done | {db.unique_company_count():,} companies | "
        f"{stats['searches_done']:,} searches{retry}\n"
        f"Last backup : {backup_label} | progress log: {ledger_count:,} entries"
    )


def format_profile_startup_banner(db: CountryDatabase) -> str:
    stats = db.profile_stats()
    size_mb = db.path.stat().st_size / (1024 * 1024) if db.path.exists() else 0.0
    last_backup = latest_backup_mtime(db.continent_code, db.country_name)
    ledger_count = count_entries(db.continent_code, db.country_name)
    backup_label = last_backup.isoformat() if last_backup else "none"
    extra = ""
    if stats.get("failed", 0):
        extra += f" | {stats['failed']} failed"
    if stats.get("not_found", 0):
        extra += f" | {stats['not_found']} not_found"
    return (
        f"Database : {db.path.resolve()} ({size_mb:.1f} MB)\n"
        f"Profiles : {stats['scraped']} scraped | {stats['pending']} pending{extra}\n"
        f"Last backup: {backup_label}\n"
        f"Ledger   : {ledger_count} entries"
    )
