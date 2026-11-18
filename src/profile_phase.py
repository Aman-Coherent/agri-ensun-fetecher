from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass

from src.config_loader import DATA_DIR, LOGS_DIR, country_slug, ensure_dirs, settings
from src.geo import Continent, Country
from src.profile_coordinator import ProfileRunCoordinator
from src.profile_http import ProfileHttpClient
from src.profile_worker import ProfileWorker, ProfileWorkerConfig
from src.session import run_async
from src.shutdown import ShutdownRequested, keep_awake_disable, keep_awake_enable, register_asyncio_loop, register_shutdown_handler
from src.storage.country_db import CountryDatabase
from src.storage.run_durability import RunDurability, format_profile_startup_banner


@dataclass
class ProfileRunConfig:
    continent: Continent
    country: Country
    force_rerun: bool = False
    limit: int | None = None
    headless: bool = True
    keep_awake: bool = False
    worker_count: int = 1


class ProfilePhaseRunner:
    def __init__(self, config: ProfileRunConfig) -> None:
        self.config = config
        self.db = CountryDatabase(config.continent.code, config.country.name)
        cfg = settings()
        self.settings_delay = float(cfg.get("profile_post_scrape_delay_seconds", 0.5))
        self._log_timings = bool(cfg.get("profile_log_timings", False))
        self._stagger = float(cfg.get("profile_worker_stagger_seconds", 0.1))
        self._interrupted = False
        log_dir = LOGS_DIR / config.continent.code
        ensure_dirs(log_dir)
        self.log_path = log_dir / f"{country_slug(config.country.name)}_profile.log"
        slug = country_slug(config.country.name)
        self._run_lock_path = DATA_DIR / config.continent.code / f"{slug}.profile.run.lock"

    def _log(self, message: str) -> None:
        try:
            print(message, flush=True)
        except UnicodeEncodeError:
            safe = message.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8")
            print(safe, flush=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(message + "\n")

    def prepare_run_summary(self) -> dict[str, int | str]:
        stats = self.db.profile_stats()
        return {
            "total": stats["total"],
            "scraped": stats["scraped"],
            "pending": stats["pending"],
            "partial": stats.get("partial", 0),
            "failed": stats.get("failed", 0),
            "not_found": stats.get("not_found", 0),
            "worker_count": self.config.worker_count,
        }

    def run(self) -> None:
        run_async(self._run_async())

    async def _run_async(self) -> None:
        register_asyncio_loop(asyncio.get_running_loop())
        coordinator = ProfileRunCoordinator(self._run_lock_path)
        coordinator.set_limit(self.config.limit)
        durability = RunDurability(self.db, self._log, mode="profile")
        register_shutdown_handler(lambda: setattr(self, "_interrupted", True))
        keep_awake = self.config.keep_awake or bool(settings().get("keep_awake", False))
        if keep_awake:
            keep_awake_enable()
            self._log("Keep-awake enabled — PC will not sleep during this run.")

        try:
            coordinator.acquire_run_lock()
        except RuntimeError as exc:
            self._log(str(exc))
            self.db.close()
            raise SystemExit(1) from exc

        try:
            self._log(format_profile_startup_banner(self.db))
            self.db.reset_profile_in_progress()

            stats = self.db.profile_stats()
            workers = max(1, self.config.worker_count)
            extra = ""
            if stats.get("failed", 0):
                extra += f" | failed={stats['failed']}"
            if stats.get("not_found", 0):
                extra += f" | not_found={stats['not_found']}"
            self._log(
                f"Profile phase: {self.config.continent.code} -> {self.config.country.name} | "
                f"workers={workers} | already_scraped={stats['scraped']} | pending={stats['pending']}{extra}"
            )

            worker_tasks = []
            http_client: ProfileHttpClient | None = None
            if bool(settings().get("profile_http_enabled", True)):
                try:
                    http_client = ProfileHttpClient(coordinator=coordinator)
                    await http_client.start()
                    # Register with coordinator so it can be recycled during VPN
                    # rotation, preventing curl_cffi CFFI socket-callback KeyErrors.
                    coordinator.register_http_client(http_client)
                    self._log("HTTP profile fetch enabled (curl_cffi)")
                except Exception as exc:
                    self._log(f"HTTP profile fetch disabled: {exc}")
                    http_client = None

            reclaim_interval = int(
                settings().get("profile_claim_reclaim_interval_seconds", 300)
            )
            failed_retry_minutes = int(
                settings().get("profile_failed_retry_minutes", 30)
            )

            async def reclaim_watchdog() -> None:
                while not self._interrupted:
                    await asyncio.sleep(reclaim_interval)
                    if self._interrupted:
                        break
                    try:
                        n = await asyncio.to_thread(
                            self.db.reclaim_stale_profile_claims_now
                        )
                        if n:
                            self._log(f"Watchdog: reclaimed {n} stale profile claim(s)")
                    except Exception as exc:
                        self._log(f"Stale-claim watchdog error: {exc}")

            async def failed_retry_watchdog() -> None:
                while not self._interrupted:
                    await asyncio.sleep(failed_retry_minutes * 60)
                    if self._interrupted:
                        break
                    try:
                        n = await asyncio.to_thread(
                            self.db.reset_failed_profiles_for_retry,
                            failed_retry_minutes,
                        )
                        if n:
                            self._log(f"Watchdog: reset {n} failed profile(s) for retry")
                    except Exception as exc:
                        self._log(f"Failed-retry watchdog error: {exc}")

            watchdog_tasks = [
                asyncio.create_task(reclaim_watchdog()),
                asyncio.create_task(failed_retry_watchdog()),
            ]

            self._log(
                f"Starting {workers} worker(s) with one shared Chromium browser..."
            )
            for i in range(workers):
                wconfig = ProfileWorkerConfig(
                    worker_id=f"w{i}",
                    worker_index=i,
                    continent_code=self.config.continent.code,
                    country_name=self.config.country.name,
                    force_rerun=self.config.force_rerun,
                    headless=self.config.headless,
                    settings_delay=self.settings_delay,
                    log_timings=self._log_timings,
                    stagger_factor=self._stagger,
                )
                worker = ProfileWorker(
                    wconfig,
                    coordinator,
                    durability,
                    self._log,
                    interrupted=lambda: self._interrupted,
                    http_client=http_client,
                )
                worker_tasks.append(worker.run())

            results = await asyncio.gather(*worker_tasks, return_exceptions=True)
            for task in watchdog_tasks:
                task.cancel()
            await asyncio.gather(*watchdog_tasks, return_exceptions=True)
            if http_client is not None:
                await http_client.close()
            scraped_total = 0
            for i, result in enumerate(results):
                if isinstance(result, (ShutdownRequested, asyncio.CancelledError)):
                    self._interrupted = True
                elif isinstance(result, Exception):
                    self._log(f"[w{i}] worker error: {result}")
                else:
                    scraped_total += int(result)

            if self._interrupted:
                self._log("Interrupted by user — progress saved. Re-run to resume.")
            else:
                final = self.db.profile_stats()
                self._log(
                    f"Finished: scraped_this_run={scraped_total} | "
                    f"total_profiles={final['scraped']} | partial={final.get('partial', 0)}"
                )
        except (KeyboardInterrupt, ShutdownRequested, asyncio.CancelledError):
            self._interrupted = True
            self._log("Interrupted by user — progress saved. Re-run to resume.")
        except Exception as exc:
            self._log(f"Profile phase crashed: {type(exc).__name__}: {exc}")
            raise
        finally:
            try:
                await coordinator.stop_shared_browser()
            except Exception as exc:
                self._log(f"Shared browser shutdown failed: {exc}")
            try:
                durability.on_run_end()
            except Exception as exc:
                self._log(f"Run end durability failed: {exc}")
            if keep_awake:
                keep_awake_disable()
            try:
                durability.db.flush_profile_checkpoint_full()
            except Exception as exc:
                self._log(f"Interrupt checkpoint failed: {exc}")
            self.db.release_all_profile_claims_for_run()
            coordinator.release_run_lock()
            self.db.close()
