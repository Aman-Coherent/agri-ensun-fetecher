from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable

from src.browser_errors import (
    BlockedError,
    CompanyNotFoundError,
    PageNotReadyError,
    is_browser_driver_error,
)
from src.graphql.parser import profile_url_candidates
from src.profile_coordinator import ProfileRunCoordinator
from src.profile_fetch import ProfileFetcher
from src.profile_http import ProfileHttpClient
from src.scraper_registry import ScraperStatusReporter
from src.session import BrowserSession
from src.storage.country_db import CountryDatabase
from src.storage.run_durability import RunDurability
from src.shutdown import ShutdownRequested


@dataclass
class ProfileWorkerConfig:
    worker_id: str
    worker_index: int
    continent_code: str
    country_name: str
    force_rerun: bool
    headless: bool
    settings_delay: float
    log_timings: bool
    stagger_factor: float


class ProfileWorker:
    def __init__(
        self,
        config: ProfileWorkerConfig,
        coordinator: ProfileRunCoordinator,
        durability: RunDurability,
        log: Callable[[str], None],
        *,
        interrupted: Callable[[], bool],
        http_client: ProfileHttpClient | None = None,
    ) -> None:
        self.config = config
        self.coordinator = coordinator
        self.durability = durability
        self._log = log
        self._interrupted = interrupted
        self.db = CountryDatabase(config.continent_code, config.country_name)
        self.browser = BrowserSession()
        scrape_id = f"{config.continent_code}/{config.country_name}-profile-{config.worker_id}"
        self.browser.set_scrape_id(scrape_id)
        self.browser.set_profile_coordinator(coordinator, config.worker_id)
        self.browser.set_interrupt_check(interrupted)
        self.status_reporter = ScraperStatusReporter(scrape_id)
        self.browser.set_status_reporter(self.status_reporter)
        self.fetcher = ProfileFetcher(
            self.browser, http_client, coordinator=coordinator
        )
        self._scraped_count = 0

    def _wlog(self, message: str) -> None:
        self._log(f"[{self.config.worker_id}] {message}")

    async def run(self) -> int:
        await self.status_reporter.start()
        self._wlog("Starting browser session...")
        await self.browser.start(headless=self.config.headless)
        self._wlog("Browser ready")
        try:
            while not self._interrupted():
                try:
                    row = await asyncio.to_thread(
                        self.db.claim_next_profile_company,
                        self.config.worker_id,
                        include_scraped=self.config.force_rerun,
                    )
                    if not row:
                        break
                    if not await self.coordinator.take_limit_slot():
                        await asyncio.to_thread(
                            self.db.release_profile_claims, self.config.worker_id
                        )
                        break
                    await self._scrape_one(row)
                except (ShutdownRequested, asyncio.CancelledError):
                    break
        finally:
            released = await asyncio.to_thread(
                self.db.release_profile_claims, self.config.worker_id
            )
            if released:
                self._wlog(f"Released {released} in-progress claim(s)")
            await self.browser.stop()
            await self.status_reporter.stop("finished")
            self.db.close()
        return self._scraped_count

    async def _scrape_one(self, row: dict) -> None:
        cid = row["company_id"]
        name = row.get("company_name") or ""

        urls: list[str] = []
        if row.get("profile_url"):
            urls.append(row["profile_url"])
        for url in profile_url_candidates(name, cid):
            if url not in urls:
                urls.append(url)

        last_error = None
        try:
            started = time.monotonic()
            profile, source = await self.fetcher.fetch_profile(cid, name, urls)
            profile["company_name"] = profile.get("company_name") or name
            save_started = time.monotonic()
            await asyncio.to_thread(self.db.save_profile, profile)
            status = profile.get("scrape_status", "completed")
            async with self.coordinator.durability_lock:
                await asyncio.to_thread(
                    self.durability.append_profile_ledger, cid, status, name
                )
            await asyncio.to_thread(self.durability.run_profile_heavy_ops)
            self._scraped_count += 1
            elapsed = time.monotonic() - started
            save_ms = int((time.monotonic() - save_started) * 1000)
            timing_suffix = (
                f" | timing={elapsed:.1f}s save={save_ms}ms source={source}"
                if self.config.log_timings
                else ""
            )
            self._wlog(
                f"Saved status={status} | "
                f"employees={len(profile.get('employees') or [])} | "
                f"locations={len(profile.get('locations') or [])}"
                f"{timing_suffix}"
            )
            delay = self.config.settings_delay
            if self.config.stagger_factor > 0:
                delay += self.config.worker_index * self.config.stagger_factor
            from src.shutdown import interruptible_sleep

            await interruptible_sleep(delay)
            return
        except CompanyNotFoundError as exc:
            self._wlog(f"NOT FOUND (permanent): {exc}")
            await asyncio.to_thread(self.db.mark_profile_not_found, cid, str(exc))
            async with self.coordinator.durability_lock:
                await asyncio.to_thread(
                    self.durability.append_profile_ledger, cid, "not_found", name
                )
            await asyncio.to_thread(self.durability.run_profile_heavy_ops)
            return
        except BlockedError as exc:
            last_error = str(exc)
            self._wlog(f"Blocked: {exc}")
        except PageNotReadyError as exc:
            last_error = str(exc)
            self._wlog(f"Not ready: {exc}")
        except Exception as exc:
            last_error = str(exc)
            if is_browser_driver_error(exc):
                self._wlog(
                    f"Browser driver died — releasing claim (not marking failed): {exc}"
                )
                try:
                    await self.browser.restart_browser()
                except Exception as restart_exc:
                    self._wlog(f"Browser restart failed: {restart_exc}")
                await asyncio.to_thread(
                    self.db.release_profile_claim, cid, self.config.worker_id
                )
                return
            self._wlog(f"Fetch failed: {exc}")

        await asyncio.to_thread(
            self.db.mark_profile_failed,
            cid,
            last_error or "All profile URL variants failed",
        )
        async with self.coordinator.durability_lock:
            await asyncio.to_thread(
                self.durability.append_profile_ledger, cid, "failed", name
            )
        await asyncio.to_thread(self.durability.run_profile_heavy_ops)
        self._wlog(f"FAILED: {last_error}")
