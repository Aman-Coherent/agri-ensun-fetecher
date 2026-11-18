"""Per-country SQLite database: smart-split search nodes + company profiles.

Search side (agri):
  search_nodes   one row per ensun search (term + country + optional filters)
  search_results companies returned by each search node
  terms_done     (country, term) pairs whose whole smart-split tree finished
  term_entities  which category/entity lists each term (List 1 / List 2)
  companies_unique  one row per company id (profile queue)

Profile side (unchanged from the nutraceutical scraper):
  company_profiles + company_* detail tables + company_profile_raw
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable

from src.config_loader import (
    DATA_DIR,
    OUTPUT_DIR,
    country_slug,
    ensure_dirs,
    location_level,
    settings,
)
from src.graphql.parser import build_profile_url
from src.graphql.profile_flatten import flatten_profile_record
from src.storage.backup import try_auto_restore
from src.storage.sqlite_durability import (
    configure_connection,
    configure_readonly_connection,
    flush_passive,
    flush_to_disk,
    integrity_check,
)

if TYPE_CHECKING:
    from src.facets import Facets
    from src.split_planner import SearchNode
    from src.terms_loader import TermRow

FINISHED_NODE_STATUSES = frozenset({"completed", "empty", "same_as_parent"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _raw_for_storage(company: dict[str, Any]) -> str:
    """Raw search-card payload, minus the 'lines' list (a duplicate of 'text')."""
    raw = company.get("raw", company)
    if isinstance(raw, dict) and "lines" in raw and "text" in raw:
        raw = {k: v for k, v in raw.items() if k != "lines"}
    return json.dumps(raw, ensure_ascii=False, default=str)


class CountryDatabase:
    def __init__(
        self,
        continent_code: str,
        country_name: str,
        *,
        auto_recover: bool = True,
        read_only: bool = False,
    ) -> None:
        self.continent_code = continent_code
        self.country_name = country_name
        self.read_only = read_only
        slug = country_slug(country_name)
        self.db_dir = DATA_DIR / continent_code
        ensure_dirs(self.db_dir, OUTPUT_DIR / continent_code)
        self.path = self.db_dir / f"{slug}.db"
        self._integrity_verified_this_open = False
        if read_only:
            self._open_readonly()
            return
        self._open_with_recovery(auto_recover)
        self._init_schema()
        self._ensure_run_meta()
        if self._integrity_verified_this_open and self.path.exists():
            stat = self.path.stat()
            self._store_integrity_cache(self.conn, stat.st_size, stat.st_mtime_ns)

    def _open_readonly(self) -> None:
        """Open strictly for reading (exports, verification).

        Never creates the file, never migrates the schema, never writes
        run_meta, and never forces a journal_mode change — eliminating the
        write-pragma failure path entirely and guaranteeing exports cannot
        mutate or corrupt the database.
        """
        if not self.path.exists():
            raise FileNotFoundError(
                f"No database found for {self.country_name} at {self.path}. "
                "Run a search (and/or profile) scrape for this country before exporting."
            )
        uri = self.path.resolve().as_uri() + "?mode=ro"
        try:
            self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=30.0)
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                f"Could not open {self.path} for reading (it may be locked by another "
                f"running process, e.g. an active scrape). Original error: {exc}"
            ) from exc
        self.conn.row_factory = sqlite3.Row
        self._db_lock = threading.RLock()
        configure_readonly_connection(self.conn)

    def _open_with_recovery(self, auto_recover: bool) -> None:
        db_existed = self.path.exists() and self.path.stat().st_size > 0
        if db_existed and auto_recover:
            stat = self.path.stat()
            db_size = stat.st_size
            db_mtime_ns = stat.st_mtime_ns
            issue: str | None = None
            probe: sqlite3.Connection | None = None
            skip_integrity = False
            try:
                probe = sqlite3.connect(self.path)
                configure_connection(probe)
                cached = self._read_integrity_cache(probe)
                if cached is not None and cached == (db_size, db_mtime_ns):
                    skip_integrity = True
                elif not skip_integrity:
                    issue = integrity_check(probe)
                    if issue is None:
                        self._integrity_verified_this_open = True
            except sqlite3.DatabaseError as exc:
                issue = str(exc)
            finally:
                if probe is not None:
                    probe.close()

            if issue:
                restored = try_auto_restore(self.continent_code, self.country_name, self.path)
                if restored:
                    print(f"RECOVERED database from backup: {restored}", flush=True)
                else:
                    raise RuntimeError(
                        f"Database integrity check failed ({issue}). "
                        f"No backup found under data/backups/{self.continent_code}/. "
                        "Do not delete .db-wal or .db-shm files while the scraper is running."
                    )
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._db_lock = threading.RLock()
        configure_connection(self.conn)

    @staticmethod
    def _read_integrity_cache(conn: sqlite3.Connection) -> tuple[int, int] | None:
        try:
            row = conn.execute(
                """
                SELECT integrity_db_size, integrity_db_mtime_ns
                FROM run_meta WHERE id = 1
                """
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None or row[0] is None or row[1] is None:
            return None
        return int(row[0]), int(row[1])

    @staticmethod
    def _store_integrity_cache(
        conn: sqlite3.Connection,
        db_size: int,
        db_mtime_ns: int,
    ) -> None:
        now = _utc_now()
        conn.execute(
            """
            INSERT INTO run_meta (
                id, continent, country, started_at, updated_at,
                integrity_db_size, integrity_db_mtime_ns, integrity_checked_at
            ) VALUES (1, '', '', ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                integrity_db_size = excluded.integrity_db_size,
                integrity_db_mtime_ns = excluded.integrity_db_mtime_ns,
                integrity_checked_at = excluded.integrity_checked_at,
                updated_at = excluded.updated_at
            """,
            (now, now, db_size, db_mtime_ns, now),
        )
        conn.commit()

    def flush_to_disk(self) -> None:
        flush_to_disk(self.conn)

    def flush_profile_checkpoint(self) -> None:
        """Commit pending writes and passive WAL checkpoint (batched during profile runs)."""
        with self._db_lock:
            flush_passive(self.conn)

    def flush_profile_checkpoint_full(self) -> None:
        """Full TRUNCATE checkpoint — run end or interrupt only."""
        with self._db_lock:
            self.flush_to_disk()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS run_meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                continent TEXT NOT NULL,
                country TEXT NOT NULL,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS search_nodes (
                node_id TEXT PRIMARY KEY,
                location_triple TEXT NOT NULL,
                location_label TEXT,
                location_level TEXT NOT NULL,
                term_key TEXT NOT NULL,
                search_term TEXT NOT NULL,
                company_type TEXT NOT NULL DEFAULT '',
                industry TEXT NOT NULL DEFAULT '',
                depth INTEGER NOT NULL DEFAULT 0,
                parent_node_id TEXT,
                expected_count INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                facet_total INTEGER,
                facets_json TEXT,
                industry_counts_json TEXT,
                result_count INTEGER NOT NULL DEFAULT 0,
                pages_fetched INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                completed_at TEXT,
                error_message TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_nodes_term ON search_nodes(location_triple, term_key);
            CREATE INDEX IF NOT EXISTS idx_nodes_status ON search_nodes(status);

            CREATE TABLE IF NOT EXISTS terms_done (
                location_triple TEXT NOT NULL,
                term_key TEXT NOT NULL,
                search_term TEXT NOT NULL,
                nodes_total INTEGER NOT NULL DEFAULT 0,
                searches_run INTEGER NOT NULL DEFAULT 0,
                capped_leaves INTEGER NOT NULL DEFAULT 0,
                unique_companies INTEGER NOT NULL DEFAULT 0,
                completed_at TEXT NOT NULL,
                PRIMARY KEY (location_triple, term_key)
            );

            CREATE TABLE IF NOT EXISTS search_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id TEXT NOT NULL,
                term_key TEXT NOT NULL,
                search_term TEXT NOT NULL,
                company_type_filter TEXT NOT NULL DEFAULT '',
                industry_filter TEXT NOT NULL DEFAULT '',
                company_id TEXT,
                company_name TEXT,
                city TEXT,
                country TEXT,
                grade TEXT,
                employee_range TEXT,
                founding_year TEXT,
                key_takeaway TEXT,
                reference_type TEXT,
                reference_title TEXT,
                reference_snippet TEXT,
                reference_url TEXT,
                logo_url TEXT,
                profile_url TEXT,
                website_url TEXT,
                state TEXT,
                description TEXT,
                continent TEXT,
                location_triple TEXT,
                location_label TEXT,
                location_level TEXT,
                raw_json TEXT,
                scraped_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_results_node ON search_results(node_id);
            CREATE INDEX IF NOT EXISTS idx_results_company ON search_results(company_id);
            CREATE INDEX IF NOT EXISTS idx_results_term ON search_results(term_key);

            CREATE TABLE IF NOT EXISTS term_entities (
                list_id TEXT NOT NULL,
                category TEXT NOT NULL,
                entity_no TEXT NOT NULL,
                entity_name TEXT NOT NULL,
                sub_group TEXT,
                term_rank INTEGER,
                position INTEGER NOT NULL,
                search_term TEXT NOT NULL,
                term_key TEXT NOT NULL,
                PRIMARY KEY (list_id, entity_no, term_key)
            );
            CREATE INDEX IF NOT EXISTS idx_term_entities_key ON term_entities(term_key);

            CREATE TABLE IF NOT EXISTS companies_unique (
                company_id TEXT PRIMARY KEY,
                company_name TEXT,
                profile_url TEXT,
                first_seen_at TEXT NOT NULL,
                profile_scraped INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS company_profiles (
                company_id TEXT PRIMARY KEY,
                profile_url TEXT,
                company_name TEXT,
                description TEXT,
                website_url TEXT,
                logo_url TEXT,
                is_managed INTEGER DEFAULT 0,
                is_claimable INTEGER DEFAULT 0,
                phone TEXT,
                email TEXT,
                city TEXT,
                state TEXT,
                country TEXT,
                main_city TEXT,
                main_state TEXT,
                main_country TEXT,
                linkedin_url TEXT,
                facebook_url TEXT,
                twitter_url TEXT,
                google_maps_url TEXT,
                overview_city TEXT,
                overview_state TEXT,
                overview_country TEXT,
                hq_city TEXT,
                hq_state TEXT,
                hq_country TEXT,
                founding_year INTEGER,
                employee_range TEXT,
                company_size_label TEXT,
                employee_total_visible INTEGER,
                working_industry TEXT,
                estimated_revenue TEXT,
                company_type TEXT,
                ownership_structure TEXT,
                location_count INTEGER,
                location_count_label TEXT,
                countries_operating_count INTEGER,
                esg_country TEXT,
                esg_overall_risk TEXT,
                esg_environment_grade TEXT,
                esg_social_grade TEXT,
                esg_governance_grade TEXT,
                specialised_areas TEXT,
                location_entries TEXT,
                company_list_names TEXT,
                certificate_names TEXT,
                certificate_statuses TEXT,
                certificate_descriptions TEXT,
                employee_names TEXT,
                employee_job_titles TEXT,
                employee_role_categories TEXT,
                employee_emails TEXT,
                employee_linkedin_urls TEXT,
                scraped_at TEXT,
                scrape_status TEXT,
                error_message TEXT,
                data_source TEXT,
                fields_missing TEXT
            );

            CREATE TABLE IF NOT EXISTS company_specialised_areas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id TEXT NOT NULL,
                area_name TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS company_certificates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id TEXT NOT NULL,
                certificate_name TEXT,
                certificate_status TEXT,
                certificate_description TEXT
            );

            CREATE TABLE IF NOT EXISTS company_lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id TEXT NOT NULL,
                list_name TEXT,
                suitable_companies_min INTEGER,
                countries_min INTEGER
            );

            CREATE TABLE IF NOT EXISTS company_locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id TEXT NOT NULL,
                location_role TEXT,
                country TEXT,
                state TEXT,
                city TEXT
            );

            CREATE TABLE IF NOT EXISTS company_employees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id TEXT NOT NULL,
                role_category TEXT,
                full_name TEXT,
                job_title TEXT,
                email TEXT,
                phone TEXT,
                linkedin_url TEXT,
                profile_image_url TEXT
            );

            CREATE TABLE IF NOT EXISTS company_contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id TEXT NOT NULL,
                contact_type TEXT,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS company_profile_raw (
                company_id TEXT PRIMARY KEY,
                graphql_json TEXT,
                dom_json TEXT,
                scraped_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS company_products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id TEXT NOT NULL REFERENCES companies_unique(company_id),
                product_name TEXT NOT NULL,
                product_category TEXT,
                supply_chain_primary TEXT,
                supply_chain_all TEXT,
                display_label TEXT,
                classification_confidence REAL,
                is_multi INTEGER,
                source_url TEXT,
                url_read_source TEXT,
                match_method TEXT,
                imported_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_profile_status ON company_profiles(scrape_status);
            CREATE INDEX IF NOT EXISTS idx_company_products_company ON company_products(company_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_company_products_dedup ON company_products(company_id, product_name);
            """
        )
        self._migrate_companies_unique()
        self._migrate_flat_profile_columns()
        self._migrate_run_meta()
        self._migrate_search_nodes()
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_cu_profile_queue
            ON companies_unique(profile_scraped, profile_scrape_status, first_seen_at)
            """
        )
        self.conn.commit()

    def _migrate_search_nodes(self) -> None:
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(search_nodes)")}
        if "industry_counts_json" not in cols:
            self.conn.execute("ALTER TABLE search_nodes ADD COLUMN industry_counts_json TEXT")
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(search_results)")}
        for name in ("website_url", "state", "description"):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE search_results ADD COLUMN {name} TEXT")

    def _migrate_run_meta(self) -> None:
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(run_meta)")}
        if "integrity_db_size" not in cols:
            self.conn.execute(
                "ALTER TABLE run_meta ADD COLUMN integrity_db_size INTEGER"
            )
        if "integrity_db_mtime_ns" not in cols:
            self.conn.execute(
                "ALTER TABLE run_meta ADD COLUMN integrity_db_mtime_ns INTEGER"
            )
        if "integrity_checked_at" not in cols:
            self.conn.execute(
                "ALTER TABLE run_meta ADD COLUMN integrity_checked_at TEXT"
            )

    def _migrate_flat_profile_columns(self) -> None:
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(company_profiles)")}
        new_cols = [
            ("specialised_areas", "TEXT"),
            ("location_entries", "TEXT"),
            ("company_list_names", "TEXT"),
            ("certificate_names", "TEXT"),
            ("certificate_statuses", "TEXT"),
            ("certificate_descriptions", "TEXT"),
            ("employee_names", "TEXT"),
            ("employee_job_titles", "TEXT"),
            ("employee_role_categories", "TEXT"),
            ("employee_emails", "TEXT"),
            ("employee_linkedin_urls", "TEXT"),
            ("main_city", "TEXT"),
            ("main_state", "TEXT"),
            ("main_country", "TEXT"),
            ("linkedin_url", "TEXT"),
            ("facebook_url", "TEXT"),
            ("twitter_url", "TEXT"),
            ("google_maps_url", "TEXT"),
        ]
        for name, col_type in new_cols:
            if name not in cols:
                self.conn.execute(f"ALTER TABLE company_profiles ADD COLUMN {name} {col_type}")
        if "main_city" in {row[1] for row in self.conn.execute("PRAGMA table_info(company_profiles)")}:
            self.conn.execute(
                """
                UPDATE company_profiles
                SET main_city = COALESCE(main_city, city, hq_city, overview_city),
                    main_state = COALESCE(main_state, state, hq_state, overview_state),
                    main_country = COALESCE(main_country, country, hq_country, overview_country)
                WHERE main_city IS NULL OR main_state IS NULL OR main_country IS NULL
                """
            )
            self.conn.execute(
                """
                UPDATE company_profiles
                SET location_entries = NULL
                WHERE location_entries LIKE 'Headquarter |%'
                   AND location_entries NOT LIKE '%; %'
                """
            )

    def _migrate_companies_unique(self) -> None:
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(companies_unique)")}
        if "profile_scrape_status" not in cols:
            self.conn.execute(
                "ALTER TABLE companies_unique ADD COLUMN profile_scrape_status TEXT DEFAULT 'pending'"
            )
        if "profile_scraped_at" not in cols:
            self.conn.execute(
                "ALTER TABLE companies_unique ADD COLUMN profile_scraped_at TEXT"
            )
        if "profile_error" not in cols:
            self.conn.execute(
                "ALTER TABLE companies_unique ADD COLUMN profile_error TEXT"
            )
        if "profile_claimed_by" not in cols:
            self.conn.execute(
                "ALTER TABLE companies_unique ADD COLUMN profile_claimed_by TEXT"
            )
        if "profile_claimed_at" not in cols:
            self.conn.execute(
                "ALTER TABLE companies_unique ADD COLUMN profile_claimed_at TEXT"
            )
        if "profile_fail_count" not in cols:
            self.conn.execute(
                "ALTER TABLE companies_unique ADD COLUMN profile_fail_count INTEGER NOT NULL DEFAULT 0"
            )

    def _ensure_run_meta(self) -> None:
        now = _utc_now()
        self.conn.execute(
            """
            INSERT INTO run_meta (id, continent, country, started_at, updated_at)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (self.continent_code, self.country_name, now, now),
        )
        self.conn.commit()

    def reset_failed_profiles_for_retry(self, older_than_minutes: int = 30) -> int:
        from datetime import datetime, timedelta, timezone

        max_fails = int(settings().get("profile_max_fail_count", 5))
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
        ).isoformat()
        cur = self.conn.execute(
            """
            UPDATE companies_unique
            SET profile_scrape_status = 'pending',
                profile_error = NULL,
                profile_claimed_by = NULL,
                profile_claimed_at = NULL
            WHERE profile_scrape_status = 'failed'
              AND profile_scraped = 0
              AND profile_scraped_at IS NOT NULL
              AND profile_scraped_at < ?
              AND profile_fail_count < ?
            """,
            (cutoff, max_fails),
        )
        self.conn.commit()
        return int(cur.rowcount)

    def reclaim_stale_profile_claims_now(self, reclaim_stale_minutes: int | None = None) -> int:
        from datetime import datetime, timedelta, timezone

        if reclaim_stale_minutes is None:
            reclaim_stale_minutes = int(
                settings().get("profile_claim_stale_minutes", 30)
            )
        stale_before = (
            datetime.now(timezone.utc) - timedelta(minutes=reclaim_stale_minutes)
        ).isoformat()
        cur = self.conn.execute(
            """
            UPDATE companies_unique
            SET profile_scrape_status = 'pending',
                profile_claimed_by = NULL,
                profile_claimed_at = NULL
            WHERE profile_scrape_status = 'in_progress'
              AND profile_scraped = 0
              AND profile_claimed_at IS NOT NULL
              AND profile_claimed_at < ?
            """,
            (stale_before,),
        )
        self.conn.commit()
        return int(cur.rowcount)

    # ------------------------------------------------------------------
    # Search side: term -> entity mapping
    # ------------------------------------------------------------------

    def save_term_entities(self, terms: Iterable["TermRow"]) -> int:
        """Record which category/entity lists each term (idempotent)."""
        rows = [
            (
                t.list_id,
                t.category,
                t.entity_no,
                t.entity_name,
                t.sub_group,
                t.rank,
                t.position,
                t.search_term,
                t.term_key,
            )
            for t in terms
        ]
        if not rows:
            return 0
        with self._db_lock:
            self.conn.executemany(
                """
                INSERT INTO term_entities (
                    list_id, category, entity_no, entity_name, sub_group,
                    term_rank, position, search_term, term_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(list_id, entity_no, term_key) DO UPDATE SET
                    category = excluded.category,
                    entity_name = excluded.entity_name,
                    sub_group = excluded.sub_group,
                    term_rank = excluded.term_rank,
                    position = excluded.position,
                    search_term = excluded.search_term
                """,
                rows,
            )
            self.conn.commit()
        return len(rows)

    # ------------------------------------------------------------------
    # Search side: nodes (one ensun search each)
    # ------------------------------------------------------------------

    def get_node(self, node_id_value: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM search_nodes WHERE node_id = ?", (node_id_value,)
        ).fetchone()
        return dict(row) if row else None

    def _insert_node_if_missing(self, node: "SearchNode") -> None:
        self.conn.execute(
            """
            INSERT INTO search_nodes (
                node_id, location_triple, location_label, location_level,
                term_key, search_term, company_type, industry, depth,
                parent_node_id, expected_count, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            ON CONFLICT(node_id) DO NOTHING
            """,
            (
                node.node_id,
                node.location_triple,
                node.location_label,
                location_level(node.location_triple),
                node.term_key,
                node.search_term,
                node.company_type,
                node.industry,
                node.depth,
                node.parent_id,
                node.expected_count,
            ),
        )

    def mark_node_in_progress(self, node: "SearchNode") -> None:
        with self._db_lock:
            self._insert_node_if_missing(node)
            self.conn.execute(
                """
                UPDATE search_nodes
                SET status = 'in_progress',
                    attempts = attempts + 1,
                    started_at = ?,
                    expected_count = COALESCE(?, expected_count)
                WHERE node_id = ?
                """,
                (_utc_now(), node.expected_count, node.node_id),
            )
            self.conn.commit()

    def save_node_results(
        self,
        node: "SearchNode",
        companies: list[dict[str, Any]],
        pages_fetched: int,
        facets: "Facets | None",
        *,
        continent: str | None = None,
    ) -> str:
        """Atomically store a finished search: result rows, unique companies,
        and the node's status/counts. Re-running a node replaces its rows."""
        status = "completed" if companies else "empty"
        now = _utc_now()
        with self._db_lock:
            try:
                self.conn.execute("BEGIN")
                self._insert_node_if_missing(node)
                self.conn.execute("DELETE FROM search_results WHERE node_id = ?", (node.node_id,))
                self._insert_result_rows(node, companies, continent or self.continent_code, now)
                self.conn.execute(
                    """
                    UPDATE search_nodes
                    SET status = ?, result_count = ?, pages_fetched = ?,
                        facet_total = ?, facets_json = ?,
                        completed_at = ?, error_message = NULL
                    WHERE node_id = ?
                    """,
                    (
                        status,
                        len(companies),
                        pages_fetched,
                        facets.total if facets is not None else None,
                        facets.to_json() if facets is not None else None,
                        now,
                        node.node_id,
                    ),
                )
                self.flush_to_disk()
            except Exception:
                self.conn.rollback()
                raise
        return status

    def _insert_result_rows(
        self,
        node: "SearchNode",
        companies: list[dict[str, Any]],
        continent: str,
        now: str,
    ) -> None:
        if not companies:
            return
        level = location_level(node.location_triple)
        result_rows: list[tuple[Any, ...]] = []
        unique_rows: list[tuple[Any, ...]] = []
        seen_unique: set[str] = set()
        for company in companies:
            company_id = company.get("company_id") or ""
            result_rows.append(
                (
                    node.node_id,
                    node.term_key,
                    node.search_term,
                    node.company_type,
                    node.industry,
                    company_id,
                    company.get("company_name"),
                    company.get("city"),
                    company.get("country"),
                    company.get("grade"),
                    company.get("employee_range"),
                    company.get("founding_year"),
                    company.get("key_takeaway"),
                    company.get("reference_type"),
                    company.get("reference_title"),
                    company.get("reference_snippet"),
                    company.get("reference_url"),
                    company.get("logo_url"),
                    company.get("profile_url"),
                    company.get("website_url"),
                    company.get("state"),
                    company.get("description"),
                    continent,
                    node.location_triple,
                    node.location_label,
                    level,
                    _raw_for_storage(company),
                    now,
                )
            )
            if company_id and company_id not in seen_unique:
                seen_unique.add(company_id)
                profile_url = company.get("profile_url")
                if not profile_url and company.get("company_name"):
                    profile_url = build_profile_url(company["company_name"], company_id)
                unique_rows.append((company_id, company.get("company_name"), profile_url, now))

        self.conn.executemany(
            """
            INSERT INTO search_results (
                node_id, term_key, search_term, company_type_filter, industry_filter,
                company_id, company_name, city, country, grade,
                employee_range, founding_year, key_takeaway, reference_type,
                reference_title, reference_snippet, reference_url, logo_url,
                profile_url, website_url, state, description, continent,
                location_triple, location_label, location_level, raw_json, scraped_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            result_rows,
        )
        if unique_rows:
            self.conn.executemany(
                """
                INSERT INTO companies_unique (company_id, company_name, profile_url, first_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(company_id) DO UPDATE SET
                    company_name = COALESCE(excluded.company_name, companies_unique.company_name),
                    profile_url = COALESCE(excluded.profile_url, companies_unique.profile_url)
                """,
                unique_rows,
            )

    def mark_node_same_as_parent(
        self,
        node: "SearchNode",
        parent_facets: "Facets | None",
        parent_result_count: int,
    ) -> None:
        """Record a filter that narrows nothing (count == parent total): no search
        is spent; the parent's counts are stored so resume plans identically."""
        now = _utc_now()
        with self._db_lock:
            self._insert_node_if_missing(node)
            self.conn.execute(
                """
                UPDATE search_nodes
                SET status = 'same_as_parent', result_count = ?, pages_fetched = 0,
                    facet_total = ?, facets_json = ?, completed_at = ?, error_message = NULL
                WHERE node_id = ? AND status NOT IN ('completed', 'empty')
                """,
                (
                    parent_result_count,
                    parent_facets.total if parent_facets is not None else None,
                    parent_facets.to_json() if parent_facets is not None else None,
                    now,
                    node.node_id,
                ),
            )
            self.conn.commit()

    def node_company_keys(self, node_id_value: str) -> frozenset[str]:
        """Companies a stored search returned (id, or 'name:...' when id-less)."""
        rows = self.conn.execute(
            """
            SELECT DISTINCT COALESCE(NULLIF(company_id, ''), 'name:' || LOWER(company_name)) AS k
            FROM search_results WHERE node_id = ?
            """,
            (node_id_value,),
        ).fetchall()
        return frozenset(r["k"] for r in rows if r["k"])

    def load_industry_counts(self, node_id_value: str) -> dict[str, int]:
        row = self.conn.execute(
            "SELECT industry_counts_json FROM search_nodes WHERE node_id = ?", (node_id_value,)
        ).fetchone()
        if not row or not row["industry_counts_json"]:
            return {}
        try:
            return {str(k): int(v) for k, v in json.loads(row["industry_counts_json"]).items()}
        except (ValueError, TypeError, json.JSONDecodeError):
            return {}

    def save_industry_counts(self, node: "SearchNode", counts: dict[str, int]) -> None:
        """Per-industry counts inside one company type (resume never re-asks)."""
        with self._db_lock:
            self._insert_node_if_missing(node)
            self.conn.execute(
                "UPDATE search_nodes SET industry_counts_json = ? WHERE node_id = ?",
                (json.dumps(counts, ensure_ascii=False, sort_keys=True), node.node_id),
            )
            self.flush_to_disk()

    def mark_node_failed(self, node: "SearchNode", message: str) -> None:
        with self._db_lock:
            self._insert_node_if_missing(node)
            self.conn.execute(
                """
                UPDATE search_nodes
                SET status = 'failed', error_message = ?, completed_at = ?
                WHERE node_id = ?
                """,
                (message[:2000], _utc_now(), node.node_id),
            )
            self.flush_to_disk()

    def reset_in_progress_nodes(self) -> int:
        """Nodes left 'in_progress' by a crash are simply searched again."""
        cur = self.conn.execute(
            "UPDATE search_nodes SET status = 'pending' WHERE status = 'in_progress'"
        )
        self.conn.commit()
        return int(cur.rowcount)

    # ------------------------------------------------------------------
    # Search side: whole terms
    # ------------------------------------------------------------------

    def load_done_term_keys(self, location_triple: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT term_key FROM terms_done WHERE location_triple = ?",
            (location_triple,),
        ).fetchall()
        return {r["term_key"] for r in rows}

    def term_unique_company_count(self, location_triple: str, term_key_value: str) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(DISTINCT COALESCE(NULLIF(company_id, ''), company_name)) AS cnt
            FROM search_results
            WHERE location_triple = ? AND term_key = ?
            """,
            (location_triple, term_key_value),
        ).fetchone()
        return int(row["cnt"]) if row else 0

    def mark_term_done(
        self,
        location_triple: str,
        term_key_value: str,
        search_term: str,
        *,
        nodes_total: int,
        searches_run: int,
        capped_leaves: int,
    ) -> int:
        unique = self.term_unique_company_count(location_triple, term_key_value)
        with self._db_lock:
            self.conn.execute(
                """
                INSERT INTO terms_done (
                    location_triple, term_key, search_term, nodes_total,
                    searches_run, capped_leaves, unique_companies, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(location_triple, term_key) DO UPDATE SET
                    nodes_total = excluded.nodes_total,
                    searches_run = terms_done.searches_run + excluded.searches_run,
                    capped_leaves = excluded.capped_leaves,
                    unique_companies = excluded.unique_companies,
                    completed_at = excluded.completed_at
                """,
                (
                    location_triple,
                    term_key_value,
                    search_term,
                    nodes_total,
                    searches_run,
                    capped_leaves,
                    unique,
                    _utc_now(),
                ),
            )
            self.flush_to_disk()
        return unique

    def clear_term_done(self, location_triple: str, term_keys: Iterable[str] | None = None) -> int:
        """Forget 'done' markers (used by --force-rerun)."""
        with self._db_lock:
            if term_keys is None:
                cur = self.conn.execute(
                    "DELETE FROM terms_done WHERE location_triple = ?", (location_triple,)
                )
            else:
                keys = list(term_keys)
                cur = self.conn.executemany(
                    "DELETE FROM terms_done WHERE location_triple = ? AND term_key = ?",
                    [(location_triple, k) for k in keys],
                )
            self.conn.commit()
            return int(cur.rowcount or 0)

    def search_stats(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM search_nodes GROUP BY status"
        ).fetchall()
        stats: dict[str, int] = {r["status"]: int(r["cnt"]) for r in rows}
        stats["nodes_total"] = sum(stats.values())
        stats["searches_done"] = stats.get("completed", 0) + stats.get("empty", 0)
        stats["terms_done"] = int(
            self.conn.execute("SELECT COUNT(*) FROM terms_done").fetchone()[0]
        )
        stats["capped_terms"] = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM terms_done WHERE capped_leaves > 0"
            ).fetchone()[0]
        )
        stats["result_rows"] = int(
            self.conn.execute("SELECT COUNT(*) FROM search_results").fetchone()[0]
        )
        return stats

    # Banner/progress compatibility with the shared durability helpers.
    def progress_stats(self) -> dict[str, int]:
        s = self.search_stats()
        return {
            "done": s["searches_done"],
            "skipped": s.get("same_as_parent", 0),
            "pending": s.get("pending", 0) + s.get("failed", 0),
            "failed": s.get("failed", 0),
            "terms_done": s["terms_done"],
            "capped_terms": s["capped_terms"],
        }

    def unique_company_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS cnt FROM companies_unique").fetchone()
        return int(row["cnt"]) if row else 0

    def profile_stats(self) -> dict[str, int]:
        total = self.unique_company_count()
        rows = self.conn.execute(
            """
            SELECT profile_scrape_status, COUNT(*) AS cnt
            FROM companies_unique
            GROUP BY profile_scrape_status
            """
        ).fetchall()
        by_status: dict[str, int] = {r["profile_scrape_status"] or "pending": r["cnt"] for r in rows}
        scraped = self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM companies_unique WHERE profile_scraped = 1"
        ).fetchone()
        partial = self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM company_profiles WHERE scrape_status = 'partial'"
        ).fetchone()
        scraped_count = int(scraped["cnt"]) if scraped else 0
        return {
            "total": total,
            "scraped": scraped_count,
            "pending": by_status.get("pending", 0),
            "in_progress": by_status.get("in_progress", 0),
            "failed": by_status.get("failed", 0),
            "not_found": by_status.get("not_found", 0),
            "partial": int(partial["cnt"]) if partial else 0,
        }

    def load_profile_queue(
        self,
        limit: int | None = None,
        include_scraped: bool = False,
    ) -> list[dict[str, Any]]:
        if include_scraped:
            sql = """
                SELECT company_id, company_name, profile_url
                FROM companies_unique
                ORDER BY first_seen_at
            """
        else:
            sql = """
                SELECT company_id, company_name, profile_url
                FROM companies_unique
                WHERE profile_scraped = 0
                ORDER BY first_seen_at
            """
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = self.conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def load_scraped_company_ids(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT company_id FROM companies_unique WHERE profile_scraped = 1"
        ).fetchall()
        return {row["company_id"] for row in rows}

    def mark_profile_in_progress(self, company_id: str) -> None:
        self.conn.execute(
            """
            UPDATE companies_unique
            SET profile_scrape_status = 'in_progress'
            WHERE company_id = ?
            """,
            (company_id,),
        )
        self.conn.commit()

    def _reclaim_stale_profile_claims(self, reclaim_stale_minutes: int) -> None:
        if reclaim_stale_minutes <= 0:
            return
        from datetime import datetime, timedelta, timezone

        stale_before = (
            datetime.now(timezone.utc) - timedelta(minutes=reclaim_stale_minutes)
        ).isoformat()
        self.conn.execute(
            """
            UPDATE companies_unique
            SET profile_scrape_status = 'pending',
                profile_claimed_by = NULL,
                profile_claimed_at = NULL
            WHERE profile_scrape_status = 'in_progress'
              AND profile_scraped = 0
              AND profile_claimed_at IS NOT NULL
              AND profile_claimed_at < ?
            """,
            (stale_before,),
        )

    def claim_next_profile_company(
        self,
        worker_id: str,
        *,
        include_scraped: bool = False,
        reclaim_stale_minutes: int | None = None,
    ) -> dict[str, Any] | None:
        with self._db_lock:
            if reclaim_stale_minutes is None:
                reclaim_stale_minutes = int(
                    settings().get("profile_claim_stale_minutes", 30)
                )
            scraped_filter = "" if include_scraped else "AND profile_scraped = 0"
            if include_scraped:
                status_filter = """
                    (
                        profile_scrape_status IS NULL
                        OR profile_scrape_status IN ('pending', 'failed', 'completed', 'partial')
                    )
                """
            else:
                status_filter = """
                    (
                        profile_scrape_status IS NULL
                        OR profile_scrape_status = 'pending'
                    )
                """
            for _ in range(3):
                try:
                    self.conn.execute("BEGIN IMMEDIATE")
                    self._reclaim_stale_profile_claims(reclaim_stale_minutes)
                    row = self.conn.execute(
                        f"""
                        SELECT company_id, company_name, profile_url
                        FROM companies_unique
                        WHERE {status_filter}
                        {scraped_filter}
                        ORDER BY first_seen_at
                        LIMIT 1
                        """
                    ).fetchone()
                    if not row:
                        self.conn.commit()
                        return None
                    now = _utc_now()
                    updated = self.conn.execute(
                        f"""
                        UPDATE companies_unique
                        SET profile_scrape_status = 'in_progress',
                            profile_claimed_by = ?,
                            profile_claimed_at = ?,
                            profile_error = NULL
                        WHERE company_id = ?
                          AND {status_filter}
                          {scraped_filter}
                        """,
                        (worker_id, now, row["company_id"]),
                    ).rowcount
                    if updated == 0:
                        self.conn.rollback()
                        continue
                    self.conn.commit()
                    return dict(row)
                except sqlite3.OperationalError:
                    try:
                        self.conn.rollback()
                    except sqlite3.Error:
                        pass
                    continue
            return None

    def release_profile_claims(self, worker_id: str) -> int:
        with self._db_lock:
            cur = self.conn.execute(
                """
                UPDATE companies_unique
                SET profile_scrape_status = 'pending',
                    profile_claimed_by = NULL,
                    profile_claimed_at = NULL
                WHERE profile_scrape_status = 'in_progress'
                  AND profile_claimed_by = ?
                  AND profile_scraped = 0
                """,
                (worker_id,),
            )
            self.conn.commit()
            return int(cur.rowcount)

    def release_profile_claim(
        self, company_id: str, worker_id: str | None = None
    ) -> bool:
        """Return one in-progress company to pending without marking failed."""
        with self._db_lock:
            if worker_id:
                cur = self.conn.execute(
                    """
                    UPDATE companies_unique
                    SET profile_scrape_status = 'pending',
                        profile_claimed_by = NULL,
                        profile_claimed_at = NULL
                    WHERE company_id = ?
                      AND profile_scrape_status = 'in_progress'
                      AND profile_claimed_by = ?
                      AND profile_scraped = 0
                    """,
                    (company_id, worker_id),
                )
            else:
                cur = self.conn.execute(
                    """
                    UPDATE companies_unique
                    SET profile_scrape_status = 'pending',
                        profile_claimed_by = NULL,
                        profile_claimed_at = NULL
                    WHERE company_id = ?
                      AND profile_scrape_status = 'in_progress'
                      AND profile_scraped = 0
                    """,
                    (company_id,),
                )
            self.conn.commit()
            return int(cur.rowcount) > 0

    def release_all_profile_claims_for_run(self) -> int:
        cur = self.conn.execute(
            """
            UPDATE companies_unique
            SET profile_scrape_status = 'pending',
                profile_claimed_by = NULL,
                profile_claimed_at = NULL
            WHERE profile_scrape_status = 'in_progress'
              AND profile_scraped = 0
            """
        )
        self.conn.commit()
        return int(cur.rowcount)

    def mark_profile_failed(self, company_id: str, error: str) -> None:
        with self._db_lock:
            now = _utc_now()
            self.conn.execute(
                """
                UPDATE companies_unique
                SET profile_scrape_status = 'failed',
                    profile_error = ?,
                    profile_scraped_at = ?,
                    profile_claimed_by = NULL,
                    profile_claimed_at = NULL,
                    profile_fail_count = profile_fail_count + 1
                WHERE company_id = ?
                """,
                (error, now, company_id),
            )
            self.conn.commit()
            flush_passive(self.conn)

    def mark_profile_not_found(self, company_id: str, error: str) -> None:
        """Permanent failure — 404/410 URL will never exist; never re-queued."""
        with self._db_lock:
            now = _utc_now()
            self.conn.execute(
                """
                UPDATE companies_unique
                SET profile_scrape_status = 'not_found',
                    profile_error = ?,
                    profile_scraped_at = ?,
                    profile_claimed_by = NULL,
                    profile_claimed_at = NULL,
                    profile_fail_count = profile_fail_count + 1
                WHERE company_id = ?
                """,
                (error, now, company_id),
            )
            self.conn.commit()
            flush_passive(self.conn)

    def save_profile(self, profile: dict[str, Any]) -> None:
        with self._db_lock:
            cid = profile["company_id"]
            now = _utc_now()
            flat = flatten_profile_record(profile)
            fields_missing = profile.get("fields_missing")
            if isinstance(fields_missing, list):
                fields_missing = json.dumps(fields_missing, ensure_ascii=False)

            self.conn.execute(
                """
                INSERT INTO company_profiles (
                    company_id, profile_url, company_name, description, website_url, logo_url,
                    is_managed, is_claimable, phone, email, city, state, country,
                    main_city, main_state, main_country,
                    linkedin_url, facebook_url, twitter_url, google_maps_url,
                    overview_city, overview_state, overview_country,
                    hq_city, hq_state, hq_country, founding_year, employee_range,
                    company_size_label, employee_total_visible, working_industry,
                    estimated_revenue, company_type, ownership_structure, location_count,
                    location_count_label, countries_operating_count,
                    specialised_areas, location_entries, company_list_names,
                    certificate_names, certificate_statuses, certificate_descriptions,
                    employee_names, employee_job_titles, employee_role_categories,
                    employee_emails, employee_linkedin_urls,
                    scraped_at, scrape_status, error_message, data_source, fields_missing
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(company_id) DO UPDATE SET
                    profile_url = COALESCE(excluded.profile_url, company_profiles.profile_url),
                    company_name = COALESCE(excluded.company_name, company_profiles.company_name),
                    description = COALESCE(excluded.description, company_profiles.description),
                    website_url = COALESCE(excluded.website_url, company_profiles.website_url),
                    logo_url = COALESCE(excluded.logo_url, company_profiles.logo_url),
                    is_managed = excluded.is_managed,
                    is_claimable = excluded.is_claimable,
                    phone = COALESCE(excluded.phone, company_profiles.phone),
                    email = COALESCE(excluded.email, company_profiles.email),
                    city = COALESCE(excluded.city, company_profiles.city),
                    state = COALESCE(excluded.state, company_profiles.state),
                    country = COALESCE(excluded.country, company_profiles.country),
                    main_city = COALESCE(excluded.main_city, company_profiles.main_city),
                    main_state = COALESCE(excluded.main_state, company_profiles.main_state),
                    main_country = COALESCE(excluded.main_country, company_profiles.main_country),
                    linkedin_url = COALESCE(excluded.linkedin_url, company_profiles.linkedin_url),
                    facebook_url = COALESCE(excluded.facebook_url, company_profiles.facebook_url),
                    twitter_url = COALESCE(excluded.twitter_url, company_profiles.twitter_url),
                    google_maps_url = COALESCE(excluded.google_maps_url, company_profiles.google_maps_url),
                    overview_city = COALESCE(excluded.overview_city, company_profiles.overview_city),
                    overview_state = COALESCE(excluded.overview_state, company_profiles.overview_state),
                    overview_country = COALESCE(excluded.overview_country, company_profiles.overview_country),
                    hq_city = COALESCE(excluded.hq_city, company_profiles.hq_city),
                    hq_state = COALESCE(excluded.hq_state, company_profiles.hq_state),
                    hq_country = COALESCE(excluded.hq_country, company_profiles.hq_country),
                    founding_year = COALESCE(excluded.founding_year, company_profiles.founding_year),
                    employee_range = COALESCE(excluded.employee_range, company_profiles.employee_range),
                    company_size_label = COALESCE(excluded.company_size_label, company_profiles.company_size_label),
                    employee_total_visible = COALESCE(excluded.employee_total_visible, company_profiles.employee_total_visible),
                    working_industry = COALESCE(excluded.working_industry, company_profiles.working_industry),
                    estimated_revenue = COALESCE(excluded.estimated_revenue, company_profiles.estimated_revenue),
                    company_type = COALESCE(excluded.company_type, company_profiles.company_type),
                    ownership_structure = COALESCE(excluded.ownership_structure, company_profiles.ownership_structure),
                    location_count = COALESCE(excluded.location_count, company_profiles.location_count),
                    location_count_label = COALESCE(excluded.location_count_label, company_profiles.location_count_label),
                    countries_operating_count = COALESCE(excluded.countries_operating_count, company_profiles.countries_operating_count),
                    specialised_areas = COALESCE(excluded.specialised_areas, company_profiles.specialised_areas),
                    location_entries = COALESCE(excluded.location_entries, company_profiles.location_entries),
                    company_list_names = COALESCE(excluded.company_list_names, company_profiles.company_list_names),
                    certificate_names = COALESCE(excluded.certificate_names, company_profiles.certificate_names),
                    certificate_statuses = COALESCE(excluded.certificate_statuses, company_profiles.certificate_statuses),
                    certificate_descriptions = COALESCE(excluded.certificate_descriptions, company_profiles.certificate_descriptions),
                    employee_names = COALESCE(excluded.employee_names, company_profiles.employee_names),
                    employee_job_titles = COALESCE(excluded.employee_job_titles, company_profiles.employee_job_titles),
                    employee_role_categories = COALESCE(excluded.employee_role_categories, company_profiles.employee_role_categories),
                    employee_emails = COALESCE(excluded.employee_emails, company_profiles.employee_emails),
                    employee_linkedin_urls = COALESCE(excluded.employee_linkedin_urls, company_profiles.employee_linkedin_urls),
                    scraped_at = excluded.scraped_at,
                    scrape_status = excluded.scrape_status,
                    error_message = excluded.error_message,
                    data_source = excluded.data_source,
                    fields_missing = excluded.fields_missing
                """,
                (
                    cid,
                    profile.get("profile_url"),
                    profile.get("company_name"),
                    profile.get("description"),
                    profile.get("website_url"),
                    profile.get("logo_url"),
                    profile.get("is_managed", 0),
                    profile.get("is_claimable", 0),
                    profile.get("phone"),
                    profile.get("email"),
                    profile.get("city"),
                    profile.get("state"),
                    profile.get("country"),
                    flat.get("main_city"),
                    flat.get("main_state"),
                    flat.get("main_country"),
                    flat.get("linkedin_url"),
                    flat.get("facebook_url"),
                    flat.get("twitter_url"),
                    flat.get("google_maps_url"),
                    profile.get("overview_city"),
                    profile.get("overview_state"),
                    profile.get("overview_country"),
                    profile.get("hq_city"),
                    profile.get("hq_state"),
                    profile.get("hq_country"),
                    profile.get("founding_year"),
                    profile.get("employee_range"),
                    profile.get("company_size_label"),
                    profile.get("employee_total_visible"),
                    profile.get("working_industry"),
                    profile.get("estimated_revenue"),
                    profile.get("company_type"),
                    profile.get("ownership_structure"),
                    profile.get("location_count"),
                    profile.get("location_count_label"),
                    profile.get("countries_operating_count"),
                    flat.get("specialised_areas"),
                    flat.get("location_entries"),
                    flat.get("company_list_names"),
                    flat.get("certificate_names"),
                    flat.get("certificate_statuses"),
                    flat.get("certificate_descriptions"),
                    flat.get("employee_names"),
                    flat.get("employee_job_titles"),
                    flat.get("employee_role_categories"),
                    flat.get("employee_emails"),
                    flat.get("employee_linkedin_urls"),
                    now,
                    profile.get("scrape_status", "completed"),
                    profile.get("error_message"),
                    profile.get("data_source"),
                    fields_missing,
                ),
            )

            self.conn.execute(
                """
                INSERT INTO company_profile_raw (company_id, graphql_json, dom_json, scraped_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(company_id) DO UPDATE SET
                    graphql_json = excluded.graphql_json,
                    dom_json = excluded.dom_json,
                    scraped_at = excluded.scraped_at
                """,
                (
                    cid,
                    json.dumps(profile.get("graphql_raw"), ensure_ascii=False)
                    if profile.get("graphql_raw")
                    else None,
                    json.dumps(profile.get("dom_raw"), ensure_ascii=False)
                    if profile.get("dom_raw")
                    else None,
                    now,
                ),
            )

            status = profile.get("scrape_status", "completed")
            self.conn.execute(
                """
                UPDATE companies_unique
                SET profile_scraped = 1,
                    profile_scrape_status = ?,
                    profile_scraped_at = ?,
                    profile_url = COALESCE(?, profile_url),
                    profile_error = NULL,
                    profile_claimed_by = NULL,
                    profile_claimed_at = NULL
                WHERE company_id = ?
                """,
                (status, now, profile.get("profile_url"), cid),
            )
            self.conn.commit()

    def reset_profile_in_progress(self) -> None:
        self.conn.execute(
            """
            UPDATE companies_unique
            SET profile_scrape_status = 'pending',
                profile_claimed_by = NULL,
                profile_claimed_at = NULL
            WHERE profile_scrape_status = 'in_progress'
            """
        )
        self.conn.commit()

    def close(self) -> None:
        if not self.read_only:
            self.flush_to_disk()
        self.conn.close()
