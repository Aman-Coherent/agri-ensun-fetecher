"""Dead VPN server: never reuse the switch that caused the failure, and never
sit on a blank cookie tab for minutes.

Live case (UAE, 2026-11-11 17:52-17:56): a switch landed on 138.199.50.129,
which could not reach ensun (net::ERR_CONNECTION_RESET). The next block
"reused" that same switch (coalesced, age 161.8 s), the cookie tab stayed on
about:blank, and one search took 238 s.
"""

from __future__ import annotations

import asyncio
import time as real_time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from src.browser_errors import PageNotReadyError, VpnServerUnreachableError
from src.search_phase import friendly_browser_message
from src.session import BrowserSession
from src.vpn_client import recent_rotate_from_status, rotate_vpn, switch_finished_before

RESET = (
    "Page.goto: net::ERR_CONNECTION_RESET at https://ensun.io/search?q=test\n"
    "Call log:\n  - navigating to \"https://ensun.io/search?q=test\""
)

CFG = {
    "ensun_base_url": "https://ensun.io",
    "user_agent": "test-agent",
    "vpn_rotate_on_block": True,
    "vpn_rotate_service_url": "http://127.0.0.1:8765",
    "vpn_rotate_timeout_seconds": 1200,
    "vpn_rotate_coalesce_seconds": 180,
    "vpn_stale_reuse_max_waits": 2,
    "vpn_rotate_settle_seconds": 5,
    "vpn_cookie_refresh_max_seconds": 120,
    "vpn_cookie_refresh_reload_seconds": 10,
    "vpn_cookie_refresh_nav_timeout_seconds": 30,
    "vpn_cookie_refresh_dead_after_failures": 2,
    "vpn_dead_server_max_switches": 2,
    "page_ready_poll_ms": 500,
}


def make_session() -> BrowserSession:
    with patch("src.session.settings", return_value=dict(CFG)):
        b = BrowserSession()
    b.settings = dict(CFG)
    b.logs = []
    b._log = b.logs.append
    b._scrape_id = "ASIA/United Arab Emirates"
    b.session_store = MagicMock()
    return b


# ---------------------------------------------------------------------------
# 1. vpn_client: only a switch made AFTER the failure began may be reused
# ---------------------------------------------------------------------------


class SwitchTimingTests(unittest.TestCase):
    def test_switch_before_failure_is_not_reused(self):
        now = 1_000_000.0
        # switch finished 161.8 s ago, failing request started 20 s ago
        self.assertTrue(switch_finished_before(161.8, now - 20, now=now))
        # switch finished 1.2 s ago, request started 330 s ago (normal case:
        # our own slow switch, picked up from /status after the POST timed out)
        self.assertFalse(switch_finished_before(1.2, now - 330, now=now))
        # unknown age or no limit -> old behaviour (not "before")
        self.assertFalse(switch_finished_before(None, now - 20, now=now))
        self.assertFalse(switch_finished_before(161.8, None, now=now))

    def test_status_reuse_respects_not_before(self):
        status = {
            "last_rotate_age_seconds": 161.8,
            "last_result": {"ok": True, "old_ip": "155.117.189.89", "new_ip": "138.199.50.129"},
        }
        # inside the 180 s window, but the switch is older than the failure
        self.assertIsNone(
            recent_rotate_from_status(status, max_age_seconds=180, not_before=real_time.time() - 20)
        )
        # without a failure time: unchanged old behaviour
        self.assertIsNotNone(recent_rotate_from_status(status, max_age_seconds=180))
        # a switch younger than the failure is still reused
        fresh = dict(status, last_rotate_age_seconds=1.2)
        self.assertIsNotNone(
            recent_rotate_from_status(fresh, max_age_seconds=180, not_before=real_time.time() - 330)
        )

    def _fake_tool(self, post_payload):
        calls = {"post": 0}

        def fake_request(method, url, *, timeout=30, data=None):
            if method == "GET" and url.endswith("/status"):
                return 200, {
                    "rotating": False,
                    "last_rotate_age_seconds": 161.8,
                    "last_result": {"ok": True, "old_ip": "155.117.189.89", "new_ip": "138.199.50.129"},
                }
            if method == "POST" and url.endswith("/rotate"):
                calls["post"] += 1
                return 200, post_payload
            return 404, {"ok": False}

        return fake_request, calls

    def test_rotate_posts_instead_of_reusing_old_switch(self):
        real_switch = {"ok": True, "old_ip": "138.199.50.129", "new_ip": "45.87.213.227"}
        fake, calls = self._fake_tool(real_switch)
        with patch("src.vpn_client._request", side_effect=fake):
            result = rotate_vpn(
                "http://127.0.0.1:8765", 10, client_id="ASIA/United Arab Emirates",
                trigger="scraper_block", poll_seconds=0.01, not_before=real_time.time() - 20,
            )
        self.assertEqual(calls["post"], 1)
        self.assertEqual(result["new_ip"], "45.87.213.227")
        self.assertFalse(result.get("stale_reuse"))

    def test_tool_side_reuse_of_old_switch_is_flagged(self):
        """The VPN app itself answers with its 161.8 s old switch -> stale_reuse."""
        tool_reuse = {
            "ok": True, "coalesced": True, "coalesce_age_seconds": 161.8,
            "old_ip": "155.117.189.89", "new_ip": "138.199.50.129",
        }
        fake, calls = self._fake_tool(tool_reuse)
        with patch("src.vpn_client._request", side_effect=fake):
            result = rotate_vpn(
                "http://127.0.0.1:8765", 10, client_id="ASIA/United Arab Emirates",
                trigger="scraper_block", poll_seconds=0.01, not_before=real_time.time() - 20,
            )
        self.assertEqual(calls["post"], 1)
        self.assertTrue(result.get("stale_reuse"))


# ---------------------------------------------------------------------------
# 2. session: wait out the VPN app's reuse window once, then a real switch
# ---------------------------------------------------------------------------


STALE = {
    "ok": True, "coalesced": True, "stale_reuse": True, "coalesce_age_seconds": 161.8,
    "old_ip": "155.117.189.89", "new_ip": "138.199.50.129",
}
REAL = {"ok": True, "old_ip": "138.199.50.129", "new_ip": "45.87.213.227", "elapsed_seconds": 10.7}


class RotateOnBlockTests(unittest.IsolatedAsyncioTestCase):
    async def _rotate(self, results, failed_since):
        b = make_session()
        sleeps: list[float] = []

        async def fake_sleep(seconds, **_k):
            sleeps.append(seconds)

        rotate = MagicMock(side_effect=results)
        with patch("src.vpn_client.is_available", return_value=True), patch(
            "src.vpn_client.get_status", return_value={}
        ), patch("src.vpn_client.rotate_vpn", rotate), patch(
            "src.shutdown.interruptible_sleep", fake_sleep
        ):
            ok = await b._rotate_vpn_on_block(Exception("x"), failed_since=failed_since)
        return b, ok, sleeps, rotate

    async def test_waits_out_reuse_window_then_switches(self):
        started = real_time.time() - 20
        b, ok, sleeps, rotate = await self._rotate([dict(STALE), dict(REAL)], started)
        self.assertTrue(ok)
        self.assertEqual(rotate.call_count, 2)
        self.assertEqual(rotate.call_args.kwargs["not_before"], started)
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 180 - 161.8 + 3, places=1)
        self.assertTrue(any(m.startswith("VPN_WAIT") for m in b.logs), b.logs)
        self.assertIn("138.199.50.129 -> 45.87.213.227", b.logs[-1])

    async def test_gives_up_if_tool_keeps_old_switch(self):
        b, ok, sleeps, rotate = await self._rotate([dict(STALE)] * 3, real_time.time() - 20)
        self.assertFalse(ok)
        self.assertEqual(rotate.call_count, 3)  # bounded: 1 + vpn_stale_reuse_max_waits
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(any("keeps giving back" in m for m in b.logs), b.logs)

    async def test_request_start_used_when_no_failure_time_given(self):
        b = make_session()
        b.note_request_started()
        started = b._request_started_at
        rotate = MagicMock(return_value=dict(REAL))
        with patch("src.vpn_client.is_available", return_value=True), patch(
            "src.vpn_client.get_status", return_value={}
        ), patch("src.vpn_client.rotate_vpn", rotate):
            self.assertTrue(await b._rotate_vpn_on_block(Exception("x")))
        self.assertEqual(rotate.call_args.kwargs["not_before"], started)

    async def test_failed_switch_reports_blocked_not_requesting(self):
        # 2026-11-16: after a failed switch the heartbeat kept telling the VPN app
        # "Requesting VPN server change" for 20 minutes of back-off.
        class Reporter:
            def __init__(self):
                self.statuses: list[tuple[str, str]] = []

            async def report(self, status, message=""):
                self.statuses.append((status, message))

        failure = {"ok": False, "error": "Proton VPN search box not found"}
        for results in ([failure], [{"ok": True, "old_ip": "1.1.1.1", "new_ip": "1.1.1.1"}]):
            b = make_session()
            reporter = Reporter()
            b.set_status_reporter(reporter)
            with patch("src.vpn_client.is_available", return_value=True), patch(
                "src.vpn_client.get_status", return_value={}
            ), patch("src.vpn_client.rotate_vpn", MagicMock(side_effect=results)):
                self.assertFalse(await b._rotate_vpn_on_block(Exception("x")))
            self.assertEqual(reporter.statuses[0][0], "rotating")
            last_status, last_message = reporter.statuses[-1]
            self.assertEqual(last_status, "blocked", reporter.statuses)
            self.assertIn("VPN change failed", last_message)


# ---------------------------------------------------------------------------
# 3. cookie tab: fast verdict on a dead server, patient on a working one
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def time(self):
        return 1_000_000.0 + self.t


class _Count:
    def __init__(self, n):
        self.n = n

    async def count(self):
        return self.n


class CookiePage:
    """``script``: one entry per goto — "ok", an Exception, or ("hang", exc)
    (raises exc after the goto's own timeout). Results become visible
    ``results_after`` seconds after a successful load (None = never)."""

    def __init__(self, clock, script, results_after=None):
        self.clock = clock
        self.script = list(script)
        self.results_after = results_after
        self.gotos: list[tuple[str, int]] = []
        self.loaded_at = None

    def is_closed(self):
        return False

    async def goto(self, url, wait_until=None, timeout=None):
        self.gotos.append((url, timeout))
        step = self.script.pop(0) if self.script else "ok"
        if isinstance(step, tuple):
            self.clock.t += timeout / 1000
            raise step[1]
        if isinstance(step, Exception):
            self.clock.t += 0.5
            raise step
        self.clock.t += 1.0
        self.loaded_at = self.clock.t

    def locator(self, _sel):
        ready = (
            self.results_after is not None
            and self.loaded_at is not None
            and self.clock.t - self.loaded_at >= self.results_after
        )
        return _Count(3 if ready else 0)

    def get_by_role(self, *_a, **_k):
        return _Count(0)

    async def wait_for_timeout(self, ms):
        self.clock.t += ms / 1000


class CookieContext:
    def __init__(self, page=None):
        self.page = page
        self.closed = False

    async def cookies(self):
        return [{"name": "_vcrcs", "value": "ok"}]

    async def close(self):
        self.closed = True

    async def new_page(self):
        return self.page


class CookieTabTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, page, clock):
        b = make_session()

        async def fake_sleep(seconds, **_k):
            clock.t += seconds

        with patch("src.session.time", types.SimpleNamespace(monotonic=clock.monotonic, time=clock.time)), patch(
            "src.shutdown.interruptible_sleep", fake_sleep
        ):
            result = await b._wait_for_cookie_tab_ready(CookieContext(page), page)
        return b, result

    async def test_connection_reset_twice_means_dead_server_within_seconds(self):
        clock = Clock()
        page = CookiePage(clock, [Exception(RESET), Exception(RESET)])
        with self.assertRaises(VpnServerUnreachableError) as ctx:
            await self._run(page, clock)
        self.assertIn("ERR_CONNECTION_RESET", str(ctx.exception))
        self.assertEqual(len(page.gotos), 2)
        self.assertLess(clock.t, 10)  # was: blank tab for up to 120 s
        self.assertTrue(all(url == "https://ensun.io/search?q=test" for url, _ in page.gotos))

    async def test_hanging_connection_gives_up_after_two_short_loads(self):
        clock = Clock()
        timeout_exc = PlaywrightTimeoutError("Page.goto: Timeout 30000ms exceeded.")
        page = CookiePage(clock, [("hang", timeout_exc), ("hang", timeout_exc)])
        with self.assertRaises(VpnServerUnreachableError):
            await self._run(page, clock)
        self.assertEqual([t for _, t in page.gotos], [30000, 30000])
        self.assertLess(clock.t, 70)

    async def test_vpn_hop_is_not_counted_as_dead(self):
        clock = Clock()
        hop = Exception("Page.goto: net::ERR_NETWORK_CHANGED at https://ensun.io/search?q=test")
        page = CookiePage(clock, [hop, hop, "ok"], results_after=4)
        b, cookies = await self._run(page, clock)
        self.assertEqual(cookies[0]["name"], "_vcrcs")
        self.assertEqual(len(page.gotos), 3)
        b.session_store.save_cookies.assert_called_once()
        self.assertIn("VPN_COOKIES: Search results detected — cookies saved", b.logs)

    async def test_working_server_is_not_reloaded_before_results_can_appear(self):
        """ensun's browser check passes by itself in ~4 s: no reload in between."""
        clock = Clock()
        page = CookiePage(clock, ["ok"], results_after=6)
        b, _ = await self._run(page, clock)
        self.assertEqual(len(page.gotos), 1)
        self.assertNotIn("VPN_COOKIES: No results yet — refreshing tab", b.logs)

    async def test_one_dead_load_then_recovery_resets_the_count(self):
        clock = Clock()
        page = CookiePage(clock, [Exception(RESET), "ok", Exception(RESET), "ok"], results_after=None)
        page.results_after = None
        # never shows results: must end with the normal 120 s timeout, not a
        # dead-server verdict (failures were never 2 in a row)
        with self.assertRaises(RuntimeError) as ctx:
            await self._run(page, clock)
        self.assertNotIsInstance(ctx.exception, VpnServerUnreachableError)
        self.assertLessEqual(len(page.gotos), 14)  # ~every 10 s, bounded

    async def test_shared_browser_closes_tab_when_server_is_dead(self):
        clock = Clock()
        page = CookiePage(clock, [Exception(RESET), Exception(RESET)])
        context = CookieContext(page)
        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=context)
        coord = MagicMock()
        coord.suspend_all_profile_browsers = AsyncMock()
        coord.ensure_shared_browser = AsyncMock(return_value=browser)
        b = make_session()
        b._profile_coordinator = coord

        async def fake_sleep(seconds, **_k):
            clock.t += seconds

        with patch("src.session.time", types.SimpleNamespace(monotonic=clock.monotonic, time=clock.time)), patch(
            "src.shutdown.interruptible_sleep", fake_sleep
        ):
            with self.assertRaises(VpnServerUnreachableError):
                await b._refresh_cookies_on_shared_browser()
        self.assertTrue(context.closed)


# ---------------------------------------------------------------------------
# 4. recovery: dead server -> switch again right away (bounded)
# ---------------------------------------------------------------------------


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def _plan(self, b):
        return b._RecoveryPlan(
            label="RATE_LIMIT", is_block=True, rate_limited=True, needs_vpn=True,
            rotate_first=True, block_cookie_path=True, refresh_cookies=True,
        )

    async def test_dead_server_after_switch_gets_another_switch(self):
        b = make_session()
        b.note_request_started()
        b._rotate_vpn_on_block = AsyncMock(return_value=True)
        b.refresh_cookies_on_block = AsyncMock(
            side_effect=[VpnServerUnreachableError("this VPN server can't reach ensun"), [{"name": "c"}]]
        )
        rotated, ok = await b._execute_recovery(Exception("429"), self._plan(b), 1, 10)
        self.assertEqual((rotated, ok), (True, True))
        self.assertEqual(b._rotate_vpn_on_block.await_count, 2)
        first, second = b._rotate_vpn_on_block.await_args_list
        self.assertIsNone(first.kwargs.get("failed_since"))  # -> request start
        self.assertIsInstance(second.kwargs.get("failed_since"), float)
        self.assertTrue(any(m.startswith("VPN_DEAD") for m in b.logs), b.logs)

    async def test_extra_switches_are_bounded(self):
        b = make_session()
        b._rotate_vpn_on_block = AsyncMock(return_value=True)
        b.refresh_cookies_on_block = AsyncMock(side_effect=VpnServerUnreachableError("dead"))
        rotated, ok = await b._execute_recovery(Exception("429"), self._plan(b), 1, 10)
        self.assertEqual((rotated, ok), (True, False))
        self.assertEqual(b._rotate_vpn_on_block.await_count, 1 + 2)
        self.assertEqual(b.refresh_cookies_on_block.await_count, 3)

    async def test_other_cookie_failures_do_not_switch_again(self):
        b = make_session()
        b._rotate_vpn_on_block = AsyncMock(return_value=True)
        b.refresh_cookies_on_block = AsyncMock(side_effect=RuntimeError("Could not get search results"))
        rotated, ok = await b._execute_recovery(Exception("429"), self._plan(b), 1, 10)
        self.assertEqual((rotated, ok), (True, False))
        self.assertEqual(b._rotate_vpn_on_block.await_count, 1)

    async def test_background_refresh_dns_failure_escalates_to_vpn(self):
        """Log line 945: ERR_NAME_NOT_RESOLVED used to waste a whole attempt."""
        b = make_session()
        b.refresh_cookies = AsyncMock(
            side_effect=Exception("Page.goto: net::ERR_NAME_NOT_RESOLVED at https://ensun.io/search?q=test")
        )
        ok, escalate = await b._refresh_session_cookies(PageNotReadyError("timeout"), 1, 10)
        self.assertEqual((ok, escalate), (False, True))

    async def test_request_start_is_set_during_recovery_and_cleared_after(self):
        b = make_session()
        b.settings["fetch_max_retries"] = 3
        seen = []

        async def recover(exc, attempt, max_retries, **_k):
            seen.append(b._request_started_at)
            return True

        b._recover_from_fetch_error = recover
        calls = {"n": 0}

        async def fetch_once(_attempt):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PageNotReadyError("net::ERR_CONNECTION_RESET")
            return "done"

        result = await b._fetch_with_block_recovery(
            fetch_once, close_before_recovery=AsyncMock(), reset_after_recovery=AsyncMock()
        )
        self.assertEqual(result, "done")
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], float)
        self.assertIsNone(b._request_started_at)


# ---------------------------------------------------------------------------
# 5. screen: plain words
# ---------------------------------------------------------------------------


class ScreenMessageTests(unittest.TestCase):
    def test_messages(self):
        self.assertEqual(
            friendly_browser_message(
                "VPN_DEAD [ASIA/United Arab Emirates]: this VPN server can't reach ensun — switching to another server"
            ),
            "VPN: this server can't reach ensun -> switching to another one",
        )
        self.assertEqual(
            friendly_browser_message(
                "VPN_WAIT [ASIA/United Arab Emirates]: the VPN app gave back the failing server "
                "138.199.50.129 (switched 162s ago) — it allows a new server in 21s, waiting"
            ),
            "VPN: still on the broken server - the VPN app allows a new one in 21s, waiting...",
        )
        for msg in (
            "VPN_COOKIES: Page did not load (Page.goto: net::ERR_CONNECTION_RESET at https://ensun.io/search?q=test)",
            "VPN_COOKIES: Page load interrupted (net::ERR_NETWORK_CHANGED) — trying again",
            "RETRY 2/10: cookie refresh failed: this VPN server can't reach ensun (x; 2 tries in a row)",
        ):
            self.assertIsNone(friendly_browser_message(msg), msg)


if __name__ == "__main__":
    unittest.main()
