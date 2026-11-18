"""Unit tests for browser error detection helpers."""

from __future__ import annotations

import unittest

from src.browser_errors import (
    BlockedError,
    FetchDiagnostics,
    PageNotReadyError,
    detect_block_signals,
    filter_false_positive_block_signals,
    filter_false_positive_profile_block_signals,
    is_browser_driver_error,
    is_real_bot_block,
    is_rate_limited,
    is_search_page_ready,
    is_verified_empty,
)


class TestIsRealBotBlock(unittest.TestCase):
    def test_loaded_page_is_not_bot_block(self) -> None:
        diag = FetchDiagnostics(
            page_status=200,
            logo_count=8,
            graphql_ok_count=2,
            block_signals=["body_captcha"],
        )
        self.assertFalse(is_real_bot_block(diag))

    def test_empty_captcha_page_is_bot_block(self) -> None:
        diag = FetchDiagnostics(
            page_status=200,
            logo_count=0,
            graphql_ok_count=0,
            block_signals=["body_hcaptcha"],
        )
        self.assertTrue(is_real_bot_block(diag))


class TestFilterFalsePositiveBlockSignals(unittest.TestCase):
    def test_challenge_in_company_text_not_blocked_when_loaded(self) -> None:
        signals = filter_false_positive_block_signals(
            ["body_challenge"],
            page_status=200,
            logo_count=8,
            heading_count=1,
            graphql_ok_count=2,
        )
        self.assertEqual(signals, [])

    def test_keeps_body_signal_when_page_empty(self) -> None:
        signals = filter_false_positive_block_signals(
            ["body_captcha"],
            page_status=200,
            logo_count=0,
            heading_count=0,
            graphql_ok_count=0,
        )
        self.assertEqual(signals, ["body_captcha"])

    def test_keeps_http_block_signals(self) -> None:
        signals = filter_false_positive_block_signals(
            ["page_http_403", "body_challenge"],
            page_status=403,
            logo_count=8,
            heading_count=1,
            graphql_ok_count=2,
        )
        self.assertEqual(signals, ["page_http_403", "body_challenge"])


class TestFilterFalsePositiveProfileBlockSignals(unittest.TestCase):
    def test_bare_403_digits_cleared_when_profile_heading_present(self) -> None:
        """Regression: an address/phone/postal-code "403" on a real, fully
        rendered profile page (h1 present, HTTP 200) must not loop forever
        as a permanent false-positive block."""
        signals = filter_false_positive_profile_block_signals(
            ["http_403_text"],
            page_status=200,
            heading_count=1,
        )
        self.assertEqual(signals, [])

    def test_kept_when_no_heading(self) -> None:
        signals = filter_false_positive_profile_block_signals(
            ["http_403_text"],
            page_status=200,
            heading_count=0,
        )
        self.assertEqual(signals, ["http_403_text"])

    def test_kept_when_not_200(self) -> None:
        signals = filter_false_positive_profile_block_signals(
            ["http_403_text"],
            page_status=403,
            heading_count=1,
        )
        self.assertEqual(signals, ["http_403_text"])

    def test_real_http_status_signal_not_cleared(self) -> None:
        signals = filter_false_positive_profile_block_signals(
            ["page_http_429"],
            page_status=200,
            heading_count=1,
        )
        self.assertEqual(signals, ["page_http_429"])


class TestIsRateLimited(unittest.TestCase):
    def test_page_status_429(self) -> None:
        self.assertTrue(is_rate_limited(page_status=429))

    def test_graphql_429(self) -> None:
        self.assertTrue(is_rate_limited(graphql_errors=[200, 429]))


class TestIsSearchPageReady(unittest.TestCase):
    def test_full_page_without_heading(self) -> None:
        self.assertTrue(
            is_search_page_ready(
                page_status=200,
                logo_count=10,
                heading_count=0,
                graphql_ok_count=0,
            )
        )

    def test_partial_results_with_graphql(self) -> None:
        """Regression: logos=1 graphql_ok=3 heading=0 must not hang in wait loop."""
        self.assertTrue(
            is_search_page_ready(
                page_status=200,
                logo_count=1,
                heading_count=0,
                graphql_ok_count=3,
            )
        )

    def test_graphql_confirmed_empty(self) -> None:
        self.assertTrue(
            is_search_page_ready(
                page_status=200,
                logo_count=0,
                heading_count=0,
                graphql_ok_count=2,
            )
        )

    def test_not_ready_on_429_with_graphql(self) -> None:
        """Regression: 429 page must not be treated as ready when graphql_ok=1."""
        self.assertFalse(
            is_search_page_ready(
                page_status=429,
                logo_count=0,
                heading_count=0,
                graphql_ok_count=1,
            )
        )

    def test_not_ready_without_signals(self) -> None:
        self.assertFalse(
            is_search_page_ready(
                page_status=200,
                logo_count=0,
                heading_count=0,
                graphql_ok_count=0,
            )
        )

    def test_404_empty_page(self) -> None:
        self.assertTrue(
            is_search_page_ready(
                page_status=404,
                logo_count=0,
                heading_count=0,
                graphql_ok_count=0,
            )
        )


class TestIsVerifiedEmpty(unittest.TestCase):
    def test_non_empty_with_companies(self) -> None:
        diag = FetchDiagnostics(heading_count=1, logo_count=0, graphql_ok_count=1)
        self.assertFalse(is_verified_empty(diag, companies_count=5))

    def test_verified_empty_search(self) -> None:
        diag = FetchDiagnostics(heading_count=1, logo_count=0, graphql_ok_count=2)
        self.assertTrue(is_verified_empty(diag, companies_count=0))

    def test_verified_empty_on_404_page(self) -> None:
        diag = FetchDiagnostics(page_status=404)
        self.assertTrue(
            is_verified_empty(diag, companies_count=0, empty_page_http_codes=[404])
        )

    def test_verified_empty_without_heading(self) -> None:
        diag = FetchDiagnostics(heading_count=0, logo_count=0, graphql_ok_count=2)
        self.assertTrue(is_verified_empty(diag, companies_count=0))

    def test_not_verified_without_graphql(self) -> None:
        diag = FetchDiagnostics(heading_count=1, logo_count=0, graphql_ok_count=0)
        self.assertFalse(is_verified_empty(diag, companies_count=0))

    def test_not_verified_with_block_signals(self) -> None:
        diag = FetchDiagnostics(
            heading_count=1,
            logo_count=0,
            graphql_ok_count=1,
            block_signals=["graphql_403"],
        )
        self.assertFalse(is_verified_empty(diag, companies_count=0))

    def test_not_verified_graphql_errors_only(self) -> None:
        diag = FetchDiagnostics(
            heading_count=1,
            logo_count=0,
            graphql_ok_count=0,
            graphql_error_count=2,
        )
        self.assertFalse(is_verified_empty(diag, companies_count=0))

    def test_not_verified_blocked_page_status(self) -> None:
        diag = FetchDiagnostics(
            page_status=403,
            heading_count=1,
            logo_count=0,
            graphql_ok_count=1,
        )
        self.assertFalse(is_verified_empty(diag, companies_count=0, block_http_codes=[403, 429]))


class TestDetectBlockSignals(unittest.TestCase):
    def test_forbidden_in_body(self) -> None:
        signals = detect_block_signals("", "Access Forbidden page", ["forbidden"])
        self.assertIn("body_forbidden", signals)

    def test_403_in_title(self) -> None:
        signals = detect_block_signals("403 Forbidden", "", [])
        self.assertIn("http_403_text", signals)

    def test_clean_page_no_signals(self) -> None:
        signals = detect_block_signals(
            "Companies for packaging",
            "10 manufacturers for packaging in Germany",
            ["forbidden", "captcha"],
        )
        self.assertEqual(signals, [])


class TestBrowserFetchErrors(unittest.TestCase):
    def test_error_carries_diagnostics(self) -> None:
        diag = FetchDiagnostics(page_status=403, block_signals=["graphql_403"])
        err = BlockedError("blocked", diagnostics=diag)
        self.assertIs(err.diagnostics, diag)
        self.assertIn("blocked", str(err))

    def test_page_not_ready_inherits_base(self) -> None:
        err = PageNotReadyError("not ready")
        self.assertIsInstance(err, BlockedError.__bases__[0])


class TestIsBrowserDriverError(unittest.TestCase):
    def test_connection_closed_from_driver(self) -> None:
        self.assertTrue(
            is_browser_driver_error(
                Exception(
                    "Browser.new_context: Connection closed while reading from the driver"
                )
            )
        )

    def test_target_closed(self) -> None:
        self.assertTrue(
            is_browser_driver_error(
                Exception("Target page, context or browser has been closed")
            )
        )

    def test_target_closed_error_type(self) -> None:
        class TargetClosedError(Exception):
            pass

        self.assertTrue(is_browser_driver_error(TargetClosedError("gone")))

    def test_page_crashed(self) -> None:
        self.assertTrue(
            is_browser_driver_error(Exception("Page.goto: Page crashed"))
        )

    def test_unrelated_error(self) -> None:
        self.assertFalse(is_browser_driver_error(Exception("HTTP 429 rate limited")))


if __name__ == "__main__":
    unittest.main()
