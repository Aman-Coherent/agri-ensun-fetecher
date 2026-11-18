"""Atomic profile claim: no duplicate companies across concurrent workers."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from src.storage.country_db import CountryDatabase


def _insert_pending(db: CountryDatabase, company_id: str, name: str) -> None:
    db.conn.execute(
        """
        INSERT INTO companies_unique (company_id, company_name, profile_url, first_seen_at)
        VALUES (?, ?, ?, datetime('now'))
        """,
        (company_id, name, f"https://ensun.io/c/{company_id}"),
    )
    db.conn.commit()


class TestProfileClaim(unittest.TestCase):
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
        self.db = CountryDatabase("EUROPE", "Testland")
        for i in range(10):
            _insert_pending(self.db, f"co-{i:03d}", f"Company {i}")

    def tearDown(self) -> None:
        self.db.close()
        for p in reversed(self._patches):
            p.stop()
        self.temp_dir.cleanup()

    def test_concurrent_claims_never_overlap(self) -> None:
        claimed: list[str] = []
        lock = threading.Lock()

        def worker(worker_id: str) -> None:
            db = CountryDatabase("EUROPE", "Testland")
            try:
                while True:
                    row = db.claim_next_profile_company(worker_id)
                    if not row:
                        break
                    with lock:
                        claimed.append(row["company_id"])
            finally:
                db.close()

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(claimed), len(set(claimed)))
        self.assertEqual(len(claimed), 10)

    def test_release_profile_claims_returns_row_to_pool(self) -> None:
        row = self.db.claim_next_profile_company("w0")
        self.assertIsNotNone(row)
        released = self.db.release_profile_claims("w0")
        self.assertEqual(released, 1)
        row2 = self.db.claim_next_profile_company("w1")
        self.assertEqual(row2["company_id"], row["company_id"])

    def test_release_profile_claim_returns_one_row(self) -> None:
        row = self.db.claim_next_profile_company("w0")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertTrue(
            self.db.release_profile_claim(row["company_id"], "w0")
        )
        row2 = self.db.claim_next_profile_company("w1")
        self.assertEqual(row2["company_id"], row["company_id"])

    def test_scraped_rows_not_claimed(self) -> None:
        self.db.conn.execute(
            """
            UPDATE companies_unique
            SET profile_scraped = 1, profile_scrape_status = 'completed'
            WHERE company_id = 'co-000'
            """
        )
        self.db.conn.commit()
        ids = set()
        for _ in range(9):
            row = self.db.claim_next_profile_company("w0")
            if not row:
                break
            ids.add(row["company_id"])
        self.assertNotIn("co-000", ids)
        self.assertEqual(len(ids), 9)


if __name__ == "__main__":
    unittest.main()
