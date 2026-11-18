"""ProfileWorker loop: empty queue exit and global limit."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.profile_coordinator import ProfileRunCoordinator
from src.profile_worker import ProfileWorker, ProfileWorkerConfig


class TestProfileWorkers(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name) / "data"
        self.data_dir.mkdir(parents=True)
        self._patches = [
            patch("src.profile_coordinator.DATA_DIR", self.data_dir),
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

    async def _close_worker_db(self, worker: ProfileWorker) -> None:
        try:
            worker.db.close()
        except Exception:
            pass

    async def test_worker_respects_global_limit(self) -> None:
        from src.storage.country_db import CountryDatabase

        db = CountryDatabase("EUROPE", "Testland")
        for i in range(5):
            db.conn.execute(
                """
                INSERT INTO companies_unique (company_id, company_name, profile_url, first_seen_at)
                VALUES (?, ?, ?, datetime('now'))
                """,
                (f"lim-{i}", f"Co {i}", f"https://ensun.io/c/lim-{i}"),
            )
        db.conn.commit()
        db.close()

        run_lock = self.data_dir / "EUROPE" / "testland.profile.run.lock"
        run_lock.parent.mkdir(parents=True, exist_ok=True)
        coordinator = ProfileRunCoordinator(run_lock)
        coordinator.set_limit(2)
        durability = MagicMock()

        config = ProfileWorkerConfig(
            worker_id="w0",
            worker_index=0,
            continent_code="EUROPE",
            country_name="Testland",
            force_rerun=False,
            headless=True,
            settings_delay=0,
            log_timings=False,
            stagger_factor=0,
        )
        worker = ProfileWorker(
            config,
            coordinator,
            durability,
            lambda _msg: None,
            interrupted=lambda: False,
        )

        profile = {
            "company_id": "x",
            "scrape_status": "completed",
            "employees": [],
            "locations": [],
        }
        worker.browser.start = AsyncMock()
        worker.browser.stop = AsyncMock()
        worker.fetcher.fetch_profile = AsyncMock(
            return_value=(
                {
                    "company_id": "x",
                    "scrape_status": "completed",
                    "employees": [],
                    "locations": [],
                },
                "http",
            )
        )
        worker.status_reporter.start = AsyncMock()
        worker.status_reporter.stop = AsyncMock()

        scraped = await worker.run()

        await self._close_worker_db(worker)
        self.assertEqual(scraped, 2)

    async def test_worker_exits_on_empty_queue(self) -> None:
        run_lock = self.data_dir / "EUROPE" / "testland.profile.run.lock"
        run_lock.parent.mkdir(parents=True, exist_ok=True)
        coordinator = ProfileRunCoordinator(run_lock)
        durability = MagicMock()
        config = ProfileWorkerConfig(
            worker_id="w0",
            worker_index=0,
            continent_code="EUROPE",
            country_name="Testland",
            force_rerun=False,
            headless=True,
            settings_delay=0,
            log_timings=False,
            stagger_factor=0,
        )
        worker = ProfileWorker(
            config,
            coordinator,
            durability,
            lambda _msg: None,
            interrupted=lambda: False,
        )
        worker.browser.start = AsyncMock()
        worker.browser.stop = AsyncMock()
        worker.fetcher.fetch_profile = AsyncMock()
        worker.status_reporter.start = AsyncMock()
        worker.status_reporter.stop = AsyncMock()

        scraped = await worker.run()
        await self._close_worker_db(worker)
        self.assertEqual(scraped, 0)
        worker.fetcher.fetch_profile.assert_not_called()


if __name__ == "__main__":
    unittest.main()
