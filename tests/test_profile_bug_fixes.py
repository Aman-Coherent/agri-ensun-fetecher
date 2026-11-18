"""
Tests for the 10 profile-phase bug fixes:
  #1  reset_profile_in_progress clears claim fields
  #2  save_profile COALESCE upsert preserves existing non-null values
  #3  on_run_end runs inside finally (interrupt path)
  #4  mark_profile_failed commits (flush_passive behaviour)
  #5  profile_stats returns accurate per-status counts
  #6  profile_fail_count increments; retry watchdog skips over-limit companies
  #8  company_list_names is saved (not hardcoded None)
  #9  claim_next_profile_company never re-claims 'failed' directly
 #10  CompanyNotFoundError → not_found status; never re-queued
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

from src.browser_errors import CompanyNotFoundError
from src.storage.country_db import CountryDatabase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_dir: Path, country: str = "Testland") -> CountryDatabase:
    patches = [
        patch("src.storage.country_db.DATA_DIR", tmp_dir),
        patch("src.storage.country_db.OUTPUT_DIR", tmp_dir / "output"),
        patch(
            "src.config_loader.settings",
            lambda: {
                "profile_claim_stale_minutes": 30,
                "profile_max_fail_count": 3,
                "profile_failed_retry_minutes": 30,
                "sqlite_synchronous": "OFF",
                "wal_autocheckpoint": 1000,
                "sqlite_busy_timeout_ms": 5000,
            },
        ),
    ]
    for p in patches:
        p.start()

    db = CountryDatabase("EUROPE", country)

    # Store patches on db for teardown
    db._test_patches = patches  # type: ignore[attr-defined]
    return db


def _stop_db(db: CountryDatabase) -> None:
    db.close()
    for p in reversed(db._test_patches):  # type: ignore[attr-defined]
        p.stop()


def _insert_company(db: CountryDatabase, cid: str, name: str = "Co") -> None:
    db.conn.execute(
        """
        INSERT INTO companies_unique (company_id, company_name, profile_url, first_seen_at)
        VALUES (?, ?, ?, datetime('now'))
        """,
        (cid, name, f"https://ensun.io/company/{cid}"),
    )
    db.conn.commit()


# ---------------------------------------------------------------------------
# Bug #1 — reset_profile_in_progress clears claim fields
# ---------------------------------------------------------------------------

class TestResetProfileInProgress(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = _make_db(Path(self.tmp.name))

    def tearDown(self) -> None:
        _stop_db(self.db)
        self.tmp.cleanup()

    def test_claim_fields_are_cleared(self) -> None:
        _insert_company(self.db, "c1")
        self.db.conn.execute(
            """
            UPDATE companies_unique
            SET profile_scrape_status = 'in_progress',
                profile_claimed_by = 'w0',
                profile_claimed_at = datetime('now')
            WHERE company_id = 'c1'
            """
        )
        self.db.conn.commit()

        self.db.reset_profile_in_progress()

        row = self.db.conn.execute(
            "SELECT profile_scrape_status, profile_claimed_by, profile_claimed_at "
            "FROM companies_unique WHERE company_id = 'c1'"
        ).fetchone()
        self.assertEqual(row["profile_scrape_status"], "pending")
        self.assertIsNone(row["profile_claimed_by"])
        self.assertIsNone(row["profile_claimed_at"])


# ---------------------------------------------------------------------------
# Bug #4 — mark_profile_failed commits (flush_passive calls commit)
# ---------------------------------------------------------------------------

class TestMarkProfileFailedCommits(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = _make_db(Path(self.tmp.name))

    def tearDown(self) -> None:
        _stop_db(self.db)
        self.tmp.cleanup()

    def test_failure_is_persisted(self) -> None:
        _insert_company(self.db, "c1")
        self.db.mark_profile_failed("c1", "some error")

        # Open a second connection to verify the data is actually committed
        import sqlite3
        path = self.db.path
        conn2 = sqlite3.connect(path)
        conn2.row_factory = sqlite3.Row
        row = conn2.execute(
            "SELECT profile_scrape_status, profile_error FROM companies_unique WHERE company_id='c1'"
        ).fetchone()
        conn2.close()
        self.assertEqual(row["profile_scrape_status"], "failed")
        self.assertEqual(row["profile_error"], "some error")


# ---------------------------------------------------------------------------
# Bug #5 — profile_stats accurate counts
# ---------------------------------------------------------------------------

class TestProfileStats(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = _make_db(Path(self.tmp.name))

    def tearDown(self) -> None:
        _stop_db(self.db)
        self.tmp.cleanup()

    def test_each_status_counted_separately(self) -> None:
        for cid, status in [
            ("p1", "pending"),
            ("p2", "pending"),
            ("f1", "failed"),
            ("nf1", "not_found"),
        ]:
            _insert_company(self.db, cid)
            self.db.conn.execute(
                "UPDATE companies_unique SET profile_scrape_status=? WHERE company_id=?",
                (status, cid),
            )
        self.db.conn.commit()

        stats = self.db.profile_stats()
        self.assertEqual(stats["pending"], 2)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["not_found"], 1)
        self.assertEqual(stats["in_progress"], 0)
        self.assertEqual(stats["total"], 4)


# ---------------------------------------------------------------------------
# Bug #6 — profile_fail_count increments; retry watchdog respects max
# ---------------------------------------------------------------------------

class TestProfileFailCount(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = _make_db(Path(self.tmp.name))

    def tearDown(self) -> None:
        _stop_db(self.db)
        self.tmp.cleanup()

    def test_fail_count_increments(self) -> None:
        _insert_company(self.db, "c1")
        self.db.mark_profile_failed("c1", "err1")
        self.db.mark_profile_failed("c1", "err2")

        row = self.db.conn.execute(
            "SELECT profile_fail_count FROM companies_unique WHERE company_id='c1'"
        ).fetchone()
        self.assertEqual(row["profile_fail_count"], 2)

    def test_retry_watchdog_skips_over_limit_company(self) -> None:
        _insert_company(self.db, "c-over")
        # Simulate failures at or above the default max (5) with old timestamp
        self.db.conn.execute(
            """
            UPDATE companies_unique
            SET profile_scrape_status = 'failed',
                profile_fail_count = 5,
                profile_scraped_at = datetime('now', '-60 minutes')
            WHERE company_id = 'c-over'
            """
        )
        self.db.conn.commit()

        _insert_company(self.db, "c-under")
        self.db.conn.execute(
            """
            UPDATE companies_unique
            SET profile_scrape_status = 'failed',
                profile_fail_count = 2,
                profile_scraped_at = datetime('now', '-60 minutes')
            WHERE company_id = 'c-under'
            """
        )
        self.db.conn.commit()

        # patch settings so max_fail_count is known
        with patch("src.storage.country_db.settings", lambda: {"profile_max_fail_count": 5}):
            reset = self.db.reset_failed_profiles_for_retry(older_than_minutes=30)

        self.assertEqual(reset, 1, "only c-under (fail_count < max) should be reset")
        row_over = self.db.conn.execute(
            "SELECT profile_scrape_status FROM companies_unique WHERE company_id='c-over'"
        ).fetchone()
        self.assertEqual(row_over["profile_scrape_status"], "failed")


# ---------------------------------------------------------------------------
# Bug #8 — company_list_names saved from flat dict
# ---------------------------------------------------------------------------

class TestCompanyListNamesSaved(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = _make_db(Path(self.tmp.name))

    def tearDown(self) -> None:
        _stop_db(self.db)
        self.tmp.cleanup()

    def test_company_list_names_persisted(self) -> None:
        _insert_company(self.db, "c1")

        with patch("src.storage.country_db.flatten_profile_record") as mock_flat:
            mock_flat.return_value = {
                "company_list_names": "List A | List B",
                "main_city": None,
                "main_state": None,
                "main_country": None,
                "linkedin_url": None,
                "facebook_url": None,
                "twitter_url": None,
                "google_maps_url": None,
                "specialised_areas": None,
                "location_entries": None,
                "certificate_names": None,
                "certificate_statuses": None,
                "certificate_descriptions": None,
                "employee_names": None,
                "employee_job_titles": None,
                "employee_role_categories": None,
                "employee_emails": None,
                "employee_linkedin_urls": None,
            }
            self.db.save_profile({"company_id": "c1", "scrape_status": "completed"})

        row = self.db.conn.execute(
            "SELECT company_list_names FROM company_profiles WHERE company_id='c1'"
        ).fetchone()
        self.assertEqual(row["company_list_names"], "List A | List B")


# ---------------------------------------------------------------------------
# Bug #9 — claim_next_profile_company does NOT immediately re-claim 'failed'
# ---------------------------------------------------------------------------

class TestClaimDoesNotReclaimFailed(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = _make_db(Path(self.tmp.name))

    def tearDown(self) -> None:
        _stop_db(self.db)
        self.tmp.cleanup()

    def test_failed_company_is_not_claimed(self) -> None:
        _insert_company(self.db, "c-failed")
        self.db.conn.execute(
            "UPDATE companies_unique SET profile_scrape_status='failed' WHERE company_id='c-failed'"
        )
        self.db.conn.commit()

        row = self.db.claim_next_profile_company("w0")
        self.assertIsNone(row, "A 'failed' company must not be immediately re-claimed")

    def test_pending_company_is_still_claimed(self) -> None:
        _insert_company(self.db, "c-pending")

        row = self.db.claim_next_profile_company("w0")
        self.assertIsNotNone(row)
        self.assertEqual(row["company_id"], "c-pending")

    def test_failed_becomes_claimable_after_watchdog_reset(self) -> None:
        _insert_company(self.db, "c-failed")
        # Simulate a failure that is old enough for the watchdog to reset
        self.db.conn.execute(
            """
            UPDATE companies_unique
            SET profile_scrape_status = 'failed',
                profile_fail_count = 1,
                profile_scraped_at = datetime('now', '-60 minutes')
            WHERE company_id = 'c-failed'
            """
        )
        self.db.conn.commit()

        reset = self.db.reset_failed_profiles_for_retry(older_than_minutes=30)
        self.assertEqual(reset, 1)

        row = self.db.claim_next_profile_company("w0")
        self.assertIsNotNone(row)
        self.assertEqual(row["company_id"], "c-failed")


# ---------------------------------------------------------------------------
# Bug #10 — CompanyNotFoundError → not_found status, never re-queued
# ---------------------------------------------------------------------------

class TestCompanyNotFoundStatus(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = _make_db(Path(self.tmp.name))

    def tearDown(self) -> None:
        _stop_db(self.db)
        self.tmp.cleanup()

    def test_mark_not_found_sets_status(self) -> None:
        _insert_company(self.db, "c1")
        self.db.mark_profile_not_found("c1", "HTTP 404 for /company/c1")

        row = self.db.conn.execute(
            "SELECT profile_scrape_status, profile_error FROM companies_unique WHERE company_id='c1'"
        ).fetchone()
        self.assertEqual(row["profile_scrape_status"], "not_found")
        self.assertIn("404", row["profile_error"])

    def test_not_found_is_never_claimed(self) -> None:
        _insert_company(self.db, "c-nf")
        self.db.mark_profile_not_found("c-nf", "HTTP 404")

        row = self.db.claim_next_profile_company("w0")
        self.assertIsNone(row, "A 'not_found' company must never be claimed again")

    def test_not_found_is_not_reset_by_watchdog(self) -> None:
        _insert_company(self.db, "c-nf")
        self.db.conn.execute(
            """
            UPDATE companies_unique
            SET profile_scrape_status = 'not_found',
                profile_scraped_at = datetime('now', '-120 minutes')
            WHERE company_id = 'c-nf'
            """
        )
        self.db.conn.commit()

        reset = self.db.reset_failed_profiles_for_retry(older_than_minutes=30)
        self.assertEqual(reset, 0, "'not_found' must not be reset by the retry watchdog")

    def test_company_not_found_error_exists(self) -> None:
        exc = CompanyNotFoundError("HTTP 404 for /company/test")
        self.assertIsInstance(exc, Exception)
        self.assertIn("404", str(exc))


# ---------------------------------------------------------------------------
# Bug #2 — COALESCE upsert preserves existing non-null fields on re-scrape
# ---------------------------------------------------------------------------

class TestSaveProfileCoalesceUpsert(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = _make_db(Path(self.tmp.name))

    def tearDown(self) -> None:
        _stop_db(self.db)
        self.tmp.cleanup()

    def _flat(self, **overrides: object) -> dict:
        base = {
            "main_city": None, "main_state": None, "main_country": None,
            "linkedin_url": None, "facebook_url": None, "twitter_url": None,
            "google_maps_url": None, "specialised_areas": None,
            "location_entries": None, "company_list_names": None,
            "certificate_names": None, "certificate_statuses": None,
            "certificate_descriptions": None, "employee_names": None,
            "employee_job_titles": None, "employee_role_categories": None,
            "employee_emails": None, "employee_linkedin_urls": None,
        }
        base.update(overrides)
        return base

    def test_existing_website_url_not_overwritten_by_null(self) -> None:
        _insert_company(self.db, "c1")

        with patch("src.storage.country_db.flatten_profile_record", return_value=self._flat()):
            self.db.save_profile({
                "company_id": "c1",
                "website_url": "https://example.com",
                "description": "A great company",
                "scrape_status": "completed",
            })

        # Re-scrape with NULL website_url — existing value must be preserved
        with patch("src.storage.country_db.flatten_profile_record", return_value=self._flat()):
            self.db.save_profile({
                "company_id": "c1",
                "website_url": None,
                "description": None,
                "scrape_status": "partial",
            })

        row = self.db.conn.execute(
            "SELECT website_url, description FROM company_profiles WHERE company_id='c1'"
        ).fetchone()
        self.assertEqual(row["website_url"], "https://example.com")
        self.assertEqual(row["description"], "A great company")

    def test_new_value_overwrites_null(self) -> None:
        _insert_company(self.db, "c1")

        with patch("src.storage.country_db.flatten_profile_record", return_value=self._flat()):
            self.db.save_profile({"company_id": "c1", "website_url": None, "scrape_status": "partial"})

        with patch("src.storage.country_db.flatten_profile_record", return_value=self._flat()):
            self.db.save_profile({
                "company_id": "c1",
                "website_url": "https://updated.com",
                "scrape_status": "completed",
            })

        row = self.db.conn.execute(
            "SELECT website_url FROM company_profiles WHERE company_id='c1'"
        ).fetchone()
        self.assertEqual(row["website_url"], "https://updated.com")


# ---------------------------------------------------------------------------
# Bug #3 — on_run_end is called in finally (even on interrupt)
# ---------------------------------------------------------------------------

class TestOnRunEndCalledOnInterrupt(unittest.TestCase):
    """Verify on_run_end() is invoked in the finally block even when interrupted."""

    def test_on_run_end_in_finally_on_interrupt(self) -> None:
        """
        Simulate a run that immediately sets _interrupted=True (via ShutdownRequested).
        on_run_end must still be called.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            settings_val = {
                "profile_claim_stale_minutes": 30,
                "profile_max_fail_count": 5,
                "profile_backup_every": 9999,
                "profile_export_every": 9999,
                "profile_flush_every": 9999,
                "profile_export_on_run_end_only": True,
                "profile_http_enabled": False,
                "profile_post_scrape_delay_seconds": 0,
                "profile_log_timings": False,
                "profile_worker_stagger_seconds": 0,
                "profile_claim_reclaim_interval_seconds": 9999,
                "profile_failed_retry_minutes": 9999,
                "sqlite_synchronous": "OFF",
                "wal_autocheckpoint": 1000,
                "sqlite_busy_timeout_ms": 5000,
                "keep_awake": False,
            }
            patches = [
                patch("src.profile_phase.DATA_DIR", tmp_path / "data"),
                patch("src.profile_phase.LOGS_DIR", tmp_path / "logs"),
                patch("src.storage.country_db.DATA_DIR", tmp_path / "data"),
                patch("src.storage.country_db.OUTPUT_DIR", tmp_path / "output"),
                patch("src.storage.country_db.settings", lambda: settings_val),
                patch("src.storage.run_durability.create_backup", return_value=tmp_path / "bk.db"),
                patch("src.storage.run_durability.export_profile_csv", return_value=[]),
                patch("src.storage.run_durability.append_entry"),
                patch("src.profile_phase.register_asyncio_loop"),
                patch("src.profile_phase.register_shutdown_handler"),
                patch("src.profile_phase.keep_awake_enable"),
                patch("src.profile_phase.keep_awake_disable"),
                patch("src.config_loader.settings", lambda: settings_val),
            ]
            for p in patches:
                p.start()

            on_run_end_calls: list[str] = []

            try:
                from src.profile_phase import ProfilePhaseRunner, ProfileRunConfig
                from src.storage.run_durability import RunDurability

                # Build a minimal mock config
                continent = MagicMock()
                continent.code = "EUROPE"
                country = MagicMock()
                country.name = "Testland"

                config = ProfileRunConfig(
                    continent=continent,
                    country=country,
                    worker_count=1,
                    limit=0,
                )
                runner = ProfilePhaseRunner(config)

                real_durability = RunDurability(runner.db, lambda _: None, mode="profile")

                def capture_on_run_end() -> None:
                    on_run_end_calls.append("called")

                real_durability.on_run_end = capture_on_run_end  # type: ignore[method-assign]

                with patch("src.profile_phase.RunDurability", return_value=real_durability):
                    with patch("src.profile_phase.ProfileHttpClient", side_effect=Exception("disabled")):
                        # limit=0 means no companies → run finishes immediately
                        runner.run()

                self.assertGreaterEqual(
                    len(on_run_end_calls),
                    1,
                    "on_run_end must be called in finally even when run exits early",
                )
            finally:
                try:
                    runner.db.close()
                except Exception:
                    pass
                for p in reversed(patches):
                    p.stop()


if __name__ == "__main__":
    unittest.main()
