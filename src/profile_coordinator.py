"""Coordination for parallel profile workers: cookies, VPN, limits, run lock."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Callable, Awaitable, TypeVar

from src.config_loader import DATA_DIR, settings
from src.shutdown import ShutdownRequested, is_shutdown_requested
from src.storage.file_lock import FileLock, FileLockTimeout
from src.storage.session_db import SessionStore

_T = TypeVar("_T")


class ProfileRunCoordinator:
    """Shared state for N profile workers in one process."""

    def __init__(self, run_lock_path: Path) -> None:
        self._run_lock_path = run_lock_path
        self._run_lock: FileLock | None = None
        self._cookie_refresh_lock_path = DATA_DIR / "cookie_refresh.lock"
        self._vpn_lock = asyncio.Lock()
        self._cookie_refresh_async_lock = asyncio.Lock()
        self._durability_lock = asyncio.Lock()
        self._limit_lock = asyncio.Lock()
        self._rate_limit_lock = asyncio.Lock()
        self._rate_limit_until = 0.0
        self._limit_remaining: int | None = None
        self._local_cookie_versions: dict[str, str | None] = {}
        self._cookie_wait = asyncio.Event()
        self._cookie_wait.set()
        self._browser_sessions: list[Any] = []
        self._browser_restart_lock = asyncio.Lock()
        self._shared_playwright: Any = None
        self._shared_browser: Any = None
        self._shared_headless: bool = True
        # Recovery gate: cleared while VPN+cookie recovery is running so that
        # workers waiting on wait_rate_limit_cooldown() don't proceed early.
        self._recovery_done = asyncio.Event()
        self._recovery_done.set()  # initially idle — no recovery in progress
        # Optional shared HTTP (curl_cffi) client — cycled during VPN rotation
        # to prevent CFFI socket-callback KeyError when network interface changes.
        self._http_client: Any = None
        # Ramp-up window right after a VPN+cookie recovery: every worker shares
        # the one freshly-trusted IP/cookie pair, so resuming at full speed
        # immediately tends to get it rate-limited again within seconds. While
        # monotonic time is under _ramp_until, HTTP requests are paced at
        # _ramp_interval instead of their normal (faster) interval.
        self._ramp_until = 0.0
        self._ramp_interval = 0.0

    def register_http_client(self, client: Any) -> None:
        """Register the shared curl_cffi HTTP client so it can be recycled on VPN rotate."""
        self._http_client = client

    async def _cycle_http_client(self) -> None:
        """Close and restart the curl_cffi session to drain dead sockets before VPN rotate."""
        if self._http_client is None:
            return
        try:
            await self._http_client.close()
        except Exception:
            pass
        try:
            await self._http_client.start()
        except Exception:
            pass

    def register_browser(self, session: Any) -> None:
        if session not in self._browser_sessions:
            self._browser_sessions.append(session)

    def unregister_browser(self, session: Any) -> None:
        self._browser_sessions = [s for s in self._browser_sessions if s is not session]

    def _shared_browser_alive(self) -> bool:
        browser = self._shared_browser
        if browser is None:
            return False
        try:
            return bool(browser.is_connected())
        except Exception:
            return False

    def shared_browser_alive(self) -> bool:
        """Public check so workers never need to trust their own stale Browser
        reference — only the coordinator's copy is authoritative."""
        return self._shared_browser_alive()

    def current_shared_browser(self) -> Any:
        return self._shared_browser

    async def _stop_shared_browser_unlocked(self) -> None:
        browser = self._shared_browser
        self._shared_browser = None
        if browser is not None:
            try:
                if browser.is_connected():
                    await browser.close()
            except Exception:
                pass
        pw = self._shared_playwright
        self._shared_playwright = None
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass

    async def _launch_shared_browser_unlocked(self, headless: bool) -> Any:
        from playwright.async_api import async_playwright

        self._shared_headless = headless
        self._shared_playwright = await async_playwright().start()
        self._shared_browser = await self._shared_playwright.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        return self._shared_browser

    async def ensure_shared_browser(self, headless: bool = True) -> Any:
        """One Chromium for all profile workers (contexts are per-worker)."""
        async with self._browser_restart_lock:
            if self._shared_browser_alive():
                return self._shared_browser
            await self._stop_shared_browser_unlocked()
            return await self._launch_shared_browser_unlocked(headless)

    async def restart_shared_browser(self, headless: bool | None = None) -> Any:
        """Relaunch the shared Chromium; closes all worker tabs first.

        When multiple workers detect a dead driver simultaneously they all queue
        here. The first one restarts; all subsequent callers find the browser
        already alive and return the existing instance without killing it again.
        """
        async with self._browser_restart_lock:
            # Guard: if another worker already restarted while we were queued,
            # reuse the new browser instead of stopping+relaunching it again.
            if self._shared_browser_alive():
                return self._shared_browser
            await self.suspend_all_profile_browsers()
            await self._stop_shared_browser_unlocked()
            return await self._launch_shared_browser_unlocked(
                self._shared_headless if headless is None else headless
            )

    async def stop_shared_browser(self) -> None:
        async with self._browser_restart_lock:
            await self.suspend_all_profile_browsers()
            await self._stop_shared_browser_unlocked()

    async def with_shared_browser_stopped(
        self,
        fn: Callable[[], Awaitable[_T]],
        headless: bool,
    ) -> _T:
        """Stop shared browser, run fn, relaunch — holds _browser_restart_lock
        throughout so no other worker can spawn a second Chromium mid-flight."""
        async with self._browser_restart_lock:
            await self.suspend_all_profile_browsers()
            await self._stop_shared_browser_unlocked()
            try:
                result = await fn()
            finally:
                self._shared_browser = await self._launch_shared_browser_unlocked(headless)
            return result

    async def suspend_all_profile_browsers(self) -> None:
        """Close every worker's open profile page/context before a network change."""
        sessions = list(self._browser_sessions)
        for session in sessions:
            close = getattr(session, "close_profile_session", None)
            if close is None:
                continue
            try:
                await close(force_cookie_save=False)
            except Exception:
                pass

    async def run_browser_restart_exclusive(
        self,
        fn: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Serialize Chromium relaunches so N workers don't spawn N browsers at once."""
        async with self._browser_restart_lock:
            return await fn()

    def acquire_run_lock(self) -> None:
        from src.storage.file_lock import clear_stale_lock

        if clear_stale_lock(self._run_lock_path):
            print(
                f"Removed stale profile run lock (dead process): {self._run_lock_path}",
                flush=True,
            )
        timeout = float(settings().get("profile_run_lock_timeout_seconds", 5))
        lock = FileLock(self._run_lock_path, timeout=timeout)
        try:
            lock.acquire()
        except FileLockTimeout as exc:
            pid = None
            if self._run_lock_path.exists():
                try:
                    pid = int(self._run_lock_path.read_text(encoding="ascii").strip())
                except (OSError, ValueError):
                    pass
            pid_hint = f" (PID {pid} still running)" if pid else ""
            raise RuntimeError(
                f"Profile run already active{pid_hint} — lock: {self._run_lock_path}. "
                "Wait for the other run to finish."
            ) from exc
        self._run_lock = lock

    def release_run_lock(self) -> None:
        if self._run_lock is not None:
            self._run_lock.release()
            self._run_lock = None

    def set_limit(self, limit: int | None) -> None:
        self._limit_remaining = limit

    async def take_limit_slot(self) -> bool:
        if self._limit_remaining is None:
            return True
        async with self._limit_lock:
            if self._limit_remaining <= 0:
                return False
            self._limit_remaining -= 1
            return True

    @property
    def durability_lock(self) -> asyncio.Lock:
        return self._durability_lock

    async def wait_for_fresh_cookies(
        self,
        session_store: SessionStore,
        worker_key: str,
    ) -> bool:
        """Wait until no cookie refresh in progress; return True if cookies changed."""
        poll = 0.25
        refresh_lock = self._cookie_refresh_lock_path
        while refresh_lock.exists():
            if is_shutdown_requested():
                raise ShutdownRequested()
            self._cookie_wait.clear()
            try:
                await asyncio.wait_for(self._cookie_wait.wait(), timeout=poll)
            except TimeoutError:
                pass

        version = session_store.get_cookie_updated_at()
        prev = self._local_cookie_versions.get(worker_key)
        self._local_cookie_versions[worker_key] = version
        if prev is None:
            return False
        return version != prev and version is not None

    def mark_cookie_refresh_started(self) -> None:
        self._cookie_wait.clear()

    async def run_cookie_refresh_exclusive(
        self,
        fn: Callable[[], Awaitable[Any]],
    ) -> Any:
        """One in-process worker at a time runs Playwright cookie refresh."""
        async with self._cookie_refresh_async_lock:
            self.mark_cookie_refresh_started()
            try:
                return await fn()
            finally:
                self.notify_cookie_refresh_done()

    def cookie_refresh_lock(self) -> FileLock:
        timeout = float(settings().get("profile_cookie_refresh_lock_seconds", 600))
        return FileLock(self._cookie_refresh_lock_path, timeout=timeout)

    async def run_vpn_exclusive(
        self,
        fn: Callable[[], Awaitable[Any]],
    ) -> Any:
        async with self._vpn_lock:
            return await fn()

    async def wait_rate_limit_cooldown(self) -> None:
        """All workers pause while a shared 429 recovery cooldown is active."""
        while True:
            if is_shutdown_requested():
                raise ShutdownRequested()
            remaining = self._rate_limit_until - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 2.0))

    async def wait_recovery_done(self) -> None:
        """Block until VPN/cookie recovery is fully complete.

        This is the companion to wait_rate_limit_cooldown(). The cooldown timer
        is only set *after* recover_fn finishes, so a worker that exits
        wait_recovery_done() is guaranteed the shared browser is back up and
        fresh cookies are saved.
        """
        while not self._recovery_done.is_set():
            if is_shutdown_requested():
                raise ShutdownRequested()
            try:
                await asyncio.wait_for(self._recovery_done.wait(), timeout=2.0)
            except TimeoutError:
                pass

    async def maybe_run_shared_recovery(
        self,
        recover_fn: Callable[[], Awaitable[None]],
    ) -> bool:
        """
        Run VPN/cookie recovery once for all workers.
        Returns True if this call ran recovery; False if another worker just did.

        Closes every worker's profile browser session before recovery so a VPN
        IP change cannot kill mid-flight pages. The shared cooldown is set
        *after* recover_fn completes so that workers waiting in
        wait_recovery_done() / wait_rate_limit_cooldown() only proceed once
        the browser is fully back up and cookies are saved.
        """
        async with self._rate_limit_lock:
            now = time.monotonic()
            if now < self._rate_limit_until:
                return False
            cfg = settings()
            pause = float(cfg.get("profile_http_rate_limit_pause_seconds", 30))
            backoff = float(cfg.get("fetch_retry_rate_limit_seconds", 90))
            per_worker_cooldown = float(
                cfg.get("vpn_recovery_cooldown_per_worker_seconds", 2)
            )
            # Signal all workers: recovery is in progress — do not proceed.
            self._recovery_done.clear()
            await self.suspend_all_profile_browsers()
            # Close the curl_cffi HTTP session so its live sockets are drained
            # cleanly before the VPN network interface changes. Without this the
            # CFFI socket callback throws KeyError on dead FDs, which surfaces as
            # a Windows Python-CFFI error popup.
            await self._cycle_http_client()
            # Brief settle so in-flight Playwright calls observe closed pages.
            await asyncio.sleep(0.5)
            try:
                await recover_fn()
            finally:
                # Set cooldown AFTER recovery completes so workers only resume
                # once the browser is back and cookies are saved. More workers
                # sharing the freshly-rotated IP need a longer settle window.
                base_cooldown = max(pause, backoff * 0.3)
                cooldown = base_cooldown + per_worker_cooldown * self.active_worker_count()
                self._rate_limit_until = time.monotonic() + cooldown
                self._start_resume_ramp()
                self._recovery_done.set()
            return True

    def notify_cookie_refresh_done(self) -> None:
        self._cookie_wait.set()
        self._local_cookie_versions.clear()

    def active_worker_count(self) -> int:
        return max(1, len(self._browser_sessions))

    def _start_resume_ramp(self) -> None:
        """Slow down shared HTTP pacing for a while after recovery completes.

        Scales with the number of active workers: more workers means more
        simultaneous requests hitting the newly-rotated IP, so they need a
        longer, gentler ramp before returning to full speed.
        """
        cfg = settings()
        base = float(cfg.get("vpn_resume_ramp_base_seconds", 15))
        per_worker = float(cfg.get("vpn_resume_ramp_seconds_per_worker", 2))
        self._ramp_interval = float(cfg.get("vpn_resume_ramp_interval_seconds", 1.0))
        ramp_seconds = base + per_worker * self.active_worker_count()
        self._ramp_until = time.monotonic() + ramp_seconds

    def min_request_interval_seconds(self) -> float:
        """Floor pacing interval for shared HTTP requests, or 0 outside ramp."""
        if time.monotonic() >= self._ramp_until:
            return 0.0
        return self._ramp_interval
