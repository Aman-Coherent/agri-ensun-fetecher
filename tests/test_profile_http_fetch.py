"""Tests for HTTP-first profile fetch and quality gate."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.browser_errors import BlockedError, PageNotReadyError
from src.profile_fetch import ProfileFetcher


class TestProfileFetcher(unittest.IsolatedAsyncioTestCase):
    async def test_http_success_skips_playwright(self) -> None:
        browser = MagicMock()
        browser.fetch_profile_page = AsyncMock()
        http = MagicMock()
        http.fetch_html = AsyncMock(
            return_value=(
                200,
                "<html><body><h1>Acme Corp</h1><a href='https://acme.com'>Website</a></body></html>",
            )
        )
        fetcher = ProfileFetcher(browser, http, http_enabled=True)

        complete = {
            "company_id": "abc1234567890123456789012",
            "company_name": "Acme Corp",
            "profile_url": "https://ensun.io/company/acme",
            "website_url": "https://acme.com",
            "scrape_status": "completed",
            "employees": [],
            "employee_total_visible": 0,
        }
        with patch.object(
            fetcher, "_try_http", AsyncMock(return_value=(complete, None))
        ):
            profile, source = await fetcher.fetch_profile(
                "abc1234567890123456789012",
                "Acme Corp",
                ["https://ensun.io/company/acme"],
            )
        self.assertEqual(source, "http")
        self.assertEqual(profile["company_name"], "Acme Corp")
        browser.fetch_profile_page.assert_not_called()

    async def test_http_incomplete_falls_back_to_playwright(self) -> None:
        browser = MagicMock()
        browser.fetch_profile_page = AsyncMock(
            return_value=([], {"company_name": "Acme", "employees": [{"full_name": "Jane"}]})
        )
        http_dom = {"employee_total_visible": 5, "website_url": "https://acme.com"}
        fetcher = ProfileFetcher(browser, MagicMock(), http_enabled=True)

        with patch.object(
            fetcher, "_try_http", AsyncMock(return_value=(None, http_dom))
        ), patch(
            "src.profile_fetch.extract_from_graphql_responses",
            return_value={},
        ), patch(
            "src.profile_fetch.merge_profile_data",
            return_value={
                "company_id": "abc1234567890123456789012",
                "company_name": "Acme",
                "profile_url": "https://ensun.io/company/acme",
                "scrape_status": "completed",
                "employees": [{"full_name": "Jane"}],
            },
        ):
            profile, source = await fetcher.fetch_profile(
                "abc1234567890123456789012",
                "Acme",
                ["https://ensun.io/company/acme"],
            )
        self.assertIn(source, ("playwright", "mixed"))
        browser.fetch_profile_page.assert_called_once()
        self.assertEqual(profile["employees"][0]["full_name"], "Jane")

    async def test_http_block_retries_then_raises(self) -> None:
        browser = MagicMock()
        browser.recover_from_block = AsyncMock(return_value=True)
        browser.fetch_profile_page = AsyncMock()
        http = MagicMock()
        http.fetch_html = AsyncMock(return_value=(429, ""))
        fetcher = ProfileFetcher(browser, http, http_enabled=True)

        cfg = {
            "profile_http_block_retries": 2,
            "fetch_max_retries": 2,
            "profile_http_enabled": True,
        }
        with patch("src.profile_fetch.settings", return_value=cfg):
            with self.assertRaises(BlockedError):
                await fetcher.fetch_profile(
                    "abc1234567890123456789012",
                    "Acme",
                    ["https://ensun.io/company/acme"],
                )
        self.assertEqual(browser.recover_from_block.await_count, 1)
        browser.fetch_profile_page.assert_not_called()

    async def test_http_block_uses_session_recovery_not_double_shared(self) -> None:
        browser = MagicMock()
        browser.recover_from_block = AsyncMock(return_value=True)
        browser.fetch_profile_page = AsyncMock()
        http = MagicMock()
        http.fetch_html = AsyncMock(return_value=(429, ""))
        coordinator = MagicMock()
        coordinator.wait_rate_limit_cooldown = AsyncMock()
        # Real ProfileRunCoordinator.wait_recovery_done is async; the old test's
        # mock predated profile_fetch awaiting it and failed for that reason.
        coordinator.wait_recovery_done = AsyncMock()
        coordinator.maybe_run_shared_recovery = AsyncMock()
        fetcher = ProfileFetcher(
            browser, http, http_enabled=True, coordinator=coordinator
        )

        cfg = {
            "profile_http_block_retries": 2,
            "fetch_max_retries": 2,
            "profile_http_enabled": True,
        }
        with patch("src.profile_fetch.settings", return_value=cfg):
            with self.assertRaises(BlockedError):
                await fetcher.fetch_profile(
                    "abc1234567890123456789012",
                    "Acme",
                    ["https://ensun.io/company/acme"],
                )
        coordinator.maybe_run_shared_recovery.assert_not_called()
        browser.recover_from_block.assert_awaited()
        self.assertGreaterEqual(coordinator.wait_rate_limit_cooldown.await_count, 1)

    async def test_404_tries_next_url(self) -> None:
        browser = MagicMock()
        browser.fetch_profile_page = AsyncMock(
            return_value=([], {"company_name": "Acme"})
        )
        fetcher = ProfileFetcher(browser, MagicMock(), http_enabled=True)

        async def fake_http(cid, name, url):
            if url.endswith("/bad"):
                raise PageNotReadyError("HTTP 404")
            return None, None

        async def fake_pw(cid, name, url, *, http_dom=None):
            return {
                "company_id": cid,
                "company_name": name,
                "profile_url": url,
                "scrape_status": "completed",
                "employees": [],
            }

        with patch.object(fetcher, "_try_http", side_effect=fake_http), patch.object(
            fetcher, "_try_playwright", side_effect=fake_pw
        ):
            profile, _source = await fetcher.fetch_profile(
                "abc1234567890123456789012",
                "Acme",
                ["https://ensun.io/company/bad", "https://ensun.io/company/good"],
            )
        self.assertEqual(profile["profile_url"], "https://ensun.io/company/good")


class TestProfileFetcherQualityGate(unittest.TestCase):
    def test_needs_fallback_for_missing_employees(self) -> None:
        merged = {
            "company_id": "x",
            "company_name": "Co",
            "profile_url": "https://ensun.io/c/x",
            "website_url": "https://co.com",
            "employee_total_visible": 10,
            "employees": [],
            "scrape_status": "partial",
        }
        gaps = ProfileFetcher._needs_playwright_fallback(merged)
        self.assertIn("employees", gaps)

    def test_complete_when_website_present(self) -> None:
        merged = {
            "company_id": "x",
            "company_name": "Co",
            "profile_url": "https://ensun.io/c/x",
            "website_url": "https://co.com",
            "description": "A real company overview with enough alphabetic content to pass validation checks.",
            "employee_total_visible": 0,
            "employees": [],
            "scrape_status": "completed",
        }
        gaps = ProfileFetcher._needs_playwright_fallback(merged)
        self.assertEqual(gaps, [])

    def test_needs_fallback_for_bad_description(self) -> None:
        merged = {
            "company_id": "x",
            "company_name": "Co",
            "profile_url": "https://ensun.io/c/x",
            "website_url": "https://co.com",
            "description": "$1e",
            "employee_total_visible": 0,
            "employees": [],
            "scrape_status": "completed",
        }
        gaps = ProfileFetcher._needs_playwright_fallback(merged)
        self.assertIn("description", gaps)


if __name__ == "__main__":
    unittest.main()
