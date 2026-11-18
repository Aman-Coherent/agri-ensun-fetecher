"""Unit tests for BrowserSession validation logic (no browser)."""

from __future__ import annotations

import unittest

from src.browser_errors import BlockedError, FetchDiagnostics, PageNotReadyError
from src.session import BrowserSession


class TestValidateSearchFetch(unittest.TestCase):
    def setUp(self) -> None:
        self.session = BrowserSession()

    def test_passes_with_companies(self) -> None:
        diag = FetchDiagnostics(heading_count=1, logo_count=5, graphql_ok_count=2)
        self.session._validate_search_fetch(diag, [{"company_name": "Acme"}])

    def test_passes_verified_empty(self) -> None:
        diag = FetchDiagnostics(heading_count=1, logo_count=0, graphql_ok_count=1)
        self.session._validate_search_fetch(diag, [])

    def test_passes_verified_empty_404(self) -> None:
        diag = FetchDiagnostics(page_status=404)
        self.session._validate_search_fetch(diag, [])

    def test_passes_verified_empty_no_heading(self) -> None:
        diag = FetchDiagnostics(heading_count=0, logo_count=0, graphql_ok_count=2)
        self.session._validate_search_fetch(diag, [])

    def test_raises_blocked_on_signals(self) -> None:
        diag = FetchDiagnostics(block_signals=["graphql_403"])
        with self.assertRaises(BlockedError):
            self.session._validate_search_fetch(diag, [])

    def test_raises_not_ready_on_suspicious_empty(self) -> None:
        diag = FetchDiagnostics(heading_count=1, logo_count=0, graphql_ok_count=0)
        with self.assertRaises(PageNotReadyError):
            self.session._validate_search_fetch(diag, [])


class TestValidateProfileFetch(unittest.TestCase):
    def setUp(self) -> None:
        self.session = BrowserSession()

    def test_passes_with_heading(self) -> None:
        diag = FetchDiagnostics(heading_count=1, graphql_ok_count=1)
        self.session._validate_profile_fetch(diag)

    def test_raises_blocked_on_signals(self) -> None:
        diag = FetchDiagnostics(block_signals=["page_http_429"], heading_count=1)
        with self.assertRaises(BlockedError):
            self.session._validate_profile_fetch(diag)

    def test_raises_not_ready_without_heading(self) -> None:
        diag = FetchDiagnostics(heading_count=0, graphql_ok_count=0)
        with self.assertRaises(PageNotReadyError):
            self.session._validate_profile_fetch(diag)


class TestRetryBackoff(unittest.TestCase):
    def test_backoff_caps_at_max(self) -> None:
        session = BrowserSession()
        base = float(session.settings.get("fetch_retry_base_seconds", 5))
        max_delay = float(session.settings.get("fetch_retry_max_seconds", 60))
        delays = [min(base * (2 ** (i)), max_delay) for i in range(10)]
        self.assertEqual(delays[0], 5)
        self.assertEqual(delays[3], 40)
        self.assertEqual(delays[-1], 60)


if __name__ == "__main__":
    unittest.main()
