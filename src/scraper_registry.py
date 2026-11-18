from __future__ import annotations

import asyncio
from collections.abc import Callable

from src.config_loader import settings
from src.vpn_client import get_status, is_available, report_scraper, stop_scraper

# Heartbeat must not overwrite these recovery statuses with "running".
_RECOVERY_STATUSES = frozenset({"blocked", "rotating", "getting_cookies"})


class ScraperStatusReporter:
    """Send running/blocked status to the local VPN automation app."""

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self._task: asyncio.Task | None = None
        self._message_provider: Callable[[], str] | None = None
        self._status = "running"
        self._message = "Running"

    @property
    def enabled(self) -> bool:
        cfg = settings()
        return bool(cfg.get("vpn_report_status", True))

    @property
    def base_url(self) -> str:
        return str(settings().get("vpn_rotate_service_url", "http://127.0.0.1:8765"))

    @property
    def heartbeat_seconds(self) -> float:
        return float(settings().get("scraper_heartbeat_seconds", 30))

    def set_message_provider(self, provider: Callable[[], str] | None) -> None:
        self._message_provider = provider

    def _warn_block_rotate_mismatch(self) -> None:
        cfg = settings()
        if not cfg.get("vpn_rotate_on_block", False):
            return
        status = get_status(self.base_url)
        if not status:
            print(
                "WARNING: VPN automation tool not reachable at "
                f"{self.base_url} — block rotate will fail until it is running."
            )
            return
        if status.get("scraper_block_rotate_enabled") is False:
            print(
                "WARNING: ensun has vpn_rotate_on_block=true but Proton VPN Auto Clicker "
                "has 'Rotate on scraper block' DISABLED. Enable the toggle in the app "
                "or block recovery will not change the VPN server."
            )

    async def start(self) -> None:
        if not self.enabled:
            return
        if not is_available(self.base_url):
            print(
                f"WARNING: VPN service unavailable at {self.base_url} — "
                "status reporting and block rotate disabled for this run."
            )
            return
        self._warn_block_rotate_mismatch()
        self._status = "running"
        self._message = "Started"
        await asyncio.to_thread(
            report_scraper,
            self.base_url,
            self.client_id,
            "running",
            "Started",
        )
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._heartbeat_loop())

    async def report(self, status: str, message: str = "") -> None:
        self._status = status
        self._message = message or status
        if not self.enabled or not is_available(self.base_url):
            return
        await asyncio.to_thread(
            report_scraper,
            self.base_url,
            self.client_id,
            status,
            message,
        )

    async def stop(self, message: str = "finished") -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._status = "stopped"
        self._message = message
        if not self.enabled or not is_available(self.base_url):
            return
        await asyncio.to_thread(
            stop_scraper,
            self.base_url,
            self.client_id,
            message,
        )

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            if not is_available(self.base_url):
                continue
            status = self._status
            if status in _RECOVERY_STATUSES:
                message = self._message or status
            else:
                status = "running"
                message = "Running"
                if self._message_provider:
                    try:
                        message = self._message_provider() or "Running"
                    except Exception:
                        message = "Running"
                self._status = status
                self._message = message
            await asyncio.to_thread(
                report_scraper,
                self.base_url,
                self.client_id,
                status,
                message,
            )
