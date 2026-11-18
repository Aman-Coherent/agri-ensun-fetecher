"""ensun's 429 browser check: wait for it to pass instead of rotating the VPN."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from src.session import BrowserSession


class _Count:
    def __init__(self, values):
        self.values = values

    async def count(self):
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


class _Nav:
    def __init__(self, status):
        self.status = status


class FakePage:
    """Logos appear after ``ready_after`` polls (None = never)."""

    def __init__(self, ready_after, reload_status=200):
        self.polls = 0
        self.ready_after = ready_after
        self.reload_status = reload_status
        self.gotos = []

    async def wait_for_timeout(self, _ms):
        self.polls += 1

    def locator(self, _sel):
        ready = self.ready_after is not None and self.polls >= self.ready_after
        return _Count([3 if ready else 0])

    async def goto(self, url, **_k):
        self.gotos.append(url)
        return _Nav(self.reload_status)


def make_session(grace=15):
    cfg = {"browser_check_grace_seconds": grace, "rate_limit_http_codes": [429]}
    with patch("src.session.settings", return_value=cfg):
        b = BrowserSession()
    b.settings = dict(cfg)
    b.logs = []
    b._log = b.logs.append
    return b


class TestBrowserCheck(unittest.TestCase):
    def test_check_passes_then_reload_same_ip(self):
        b = make_session()
        b._page_status = 429
        page = FakePage(ready_after=7)  # ~3.5 s, as seen live
        asyncio.run(b._pass_browser_check_and_reload(page, "https://ensun.io/search?q=x"))
        self.assertEqual(page.gotos, ["https://ensun.io/search?q=x"])
        self.assertEqual(b._page_status, 200)  # no longer looks rate-limited
        self.assertTrue(any("passed" in m for m in b.logs))

    def test_check_never_passes_keeps_429_for_normal_block_recovery(self):
        b = make_session(grace=2)
        b._page_status = 429
        page = FakePage(ready_after=None)
        asyncio.run(b._pass_browser_check_and_reload(page, "u"))
        self.assertEqual(page.gotos, [])
        self.assertEqual(b._page_status, 429)
        self.assertTrue(any("treating as a block" in m for m in b.logs))

    def test_normal_page_untouched(self):
        b = make_session()
        b._page_status = 200
        page = FakePage(ready_after=0)
        asyncio.run(b._pass_browser_check_and_reload(page, "u"))
        self.assertEqual((page.polls, page.gotos), (0, []))

    def test_grace_zero_restores_old_behaviour(self):
        b = make_session(grace=0)
        b._page_status = 429
        page = FakePage(ready_after=0)
        asyncio.run(b._pass_browser_check_and_reload(page, "u"))
        self.assertEqual((page.polls, b._page_status), (0, 429))


class _RefusedPage(FakePage):
    async def title(self):
        return "Forbidden"

    async def inner_text(self, _sel):
        return "Forbidden"


class TestForbiddenFailsFast(unittest.TestCase):
    def test_403_goes_to_recovery_after_one_poll(self):
        from src.browser_errors import BlockedError

        cfg = {
            "rate_limit_http_codes": [429],
            "block_http_codes": [400, 402, 403, 429, 502, 503],
            "page_ready_max_polls": 30,
            "page_ready_poll_ms": 700,
            "block_body_patterns": ["forbidden"],
        }
        with patch("src.session.settings", return_value=cfg):
            b = BrowserSession()
        b.settings = dict(cfg)
        b._page_status = 403
        page = _RefusedPage(ready_after=None)
        with self.assertRaises(BlockedError) as ctx:
            asyncio.run(b._wait_for_page_ready(page))
        self.assertEqual(page.polls, 1)  # not 30
        self.assertIn("page_http_403", ctx.exception.diagnostics.block_signals)


if __name__ == "__main__":
    unittest.main()
