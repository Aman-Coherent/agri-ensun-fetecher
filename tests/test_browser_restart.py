"""Browser driver-death recovery restarts Chromium instead of looping context resets."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.session import BrowserSession


class TestBrowserRestartRecovery(unittest.TestCase):
    def test_driver_error_triggers_restart_browser(self) -> None:
        session = BrowserSession()
        session._browser = MagicMock()
        session._browser.is_connected.return_value = False
        session._playwright = MagicMock()
        session.restart_browser = AsyncMock()

        async def _run() -> None:
            ok = await session._recover_from_fetch_error(
                Exception(
                    "Browser.new_context: Connection closed while reading from the driver"
                ),
                attempt=1,
                max_retries=10,
                close_before_recovery=AsyncMock(),
                reset_after_recovery=AsyncMock(),
            )
            self.assertTrue(ok)
            session.restart_browser.assert_awaited_once()

        asyncio.run(_run())

    def test_unrelated_soft_error_does_not_restart(self) -> None:
        session = BrowserSession()
        session.restart_browser = AsyncMock()
        soft_reset = AsyncMock()

        async def _run() -> None:
            ok = await session._recover_from_fetch_error(
                Exception("some random playwright glitch"),
                attempt=1,
                max_retries=10,
                close_before_recovery=AsyncMock(),
                reset_after_recovery=AsyncMock(),
                reset_on_soft_error=soft_reset,
            )
            self.assertTrue(ok)
            session.restart_browser.assert_not_awaited()
            soft_reset.assert_awaited_once()

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
