"""Shared HTTP client for profile fetching via curl_cffi."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from src.config_loader import settings
from src.storage.session_db import SessionStore

try:
    from curl_cffi.requests import AsyncSession
except ImportError:  # pragma: no cover
    AsyncSession = None  # type: ignore[misc, assignment]


class ProfileHttpError(Exception):
    pass


class ProfileHttpClient:
    """curl_cffi session with saved cookies and global concurrency pacing."""

    def __init__(self, coordinator: Any | None = None) -> None:
        self._session: Any = None
        self._semaphore: asyncio.Semaphore | None = None
        self._min_interval = float(
            settings().get("profile_http_min_interval_seconds", 0.07)
        )
        self._last_request_at = 0.0
        self._interval_lock = asyncio.Lock()
        self._user_agent = settings().get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        # Coordinator can temporarily raise the effective pacing interval for
        # a ramp-up window right after a VPN rotate + cookie refresh.
        self._coordinator = coordinator

    async def start(self) -> None:
        if AsyncSession is None:
            raise ProfileHttpError("curl_cffi is not installed")
        max_concurrent = int(settings().get("profile_http_max_concurrent", 12))
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._session = AsyncSession(impersonate="chrome131")

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _load_cookie_header(self) -> str:
        store = SessionStore()
        try:
            cookies = store.load_cookies() or []
        finally:
            store.close()
        parts = []
        for c in cookies:
            name = c.get("name")
            value = c.get("value")
            if name and value:
                parts.append(f"{name}={value}")
        return "; ".join(parts)

    async def _pace(self) -> None:
        async with self._interval_lock:
            interval = self._min_interval
            if self._coordinator is not None:
                interval = max(interval, self._coordinator.min_request_interval_seconds())
            now = time.monotonic()
            wait = interval - (now - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()

    async def fetch_html(self, url: str) -> tuple[int, str]:
        if self._session is None or self._semaphore is None:
            raise ProfileHttpError("HTTP client not started")
        async with self._semaphore:
            await self._pace()
            headers = {
                "User-Agent": self._user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            }
            cookie_header = self._load_cookie_header()
            if cookie_header:
                headers["Cookie"] = cookie_header
            response = await self._session.get(
                url,
                headers=headers,
                timeout=30,
                allow_redirects=True,
            )
            status = int(response.status_code)
            text = response.text if response.text else ""
            return status, text
