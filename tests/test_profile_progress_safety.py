"""Profile phase progress safety after interrupt."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.storage.country_db import CountryDatabase


class TestProfileProgressSafety(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name) / "data"
        self.data_dir.mkdir(parents=True)
        self._patches = [
            patch("src.storage.country_db.DATA_DIR", self.data_dir),
            patch("src.storage.country_db.OUTPUT_DIR", self.data_dir / "output"),
            patch(
                "src.config_loader.settings",
                lambda: {"profile_claim_stale_minutes": 30},
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self.temp_dir.cleanup()

    def test_save_profile_marks_scraped_and_checkpoint_full_preserves_count(self) -> None:
        db = CountryDatabase("EUROPE", "Testland")
        cid = "abc1234567890123456789012"
        db.conn.execute(
            """
            INSERT INTO companies_unique (company_id, company_name, profile_url, first_seen_at)
            VALUES (?, ?, ?, datetime('now'))
            """,
            (cid, "Acme", f"https://ensun.io/company/{cid}"),
        )
        db.conn.commit()

        profile = {
            "company_id": cid,
            "company_name": "Acme",
            "profile_url": f"https://ensun.io/company/{cid}",
            "scrape_status": "completed",
            "employees": [],
            "locations": [],
            "certificates": [],
            "specialised_areas": [],
            "contacts": [],
        }
        db.save_profile(profile)
        scraped_before = db.profile_stats()["scraped"]
        self.assertEqual(scraped_before, 1)

        db.flush_profile_checkpoint_full()
        scraped_after = db.profile_stats()["scraped"]
        self.assertEqual(scraped_after, scraped_before)

        row = db.conn.execute(
            "SELECT company_id FROM company_profiles WHERE company_id = ?",
            (cid,),
        ).fetchone()
        self.assertIsNotNone(row)
        db.close()


if __name__ == "__main__":
    unittest.main()
