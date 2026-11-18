"""Retry when pagination stalls before collecting what ensun's count says exists."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from src.browser_errors import FetchDiagnostics, PageNotReadyError
from src.facets import Facets
from src.session import BrowserSession

SETTINGS = {"incomplete_results_tolerance": 2, "count_mismatch_max_attempts": 3}


def cos(n: int) -> list[dict]:
    return [{"company_id": f"{i:024x}"} for i in range(n)]


class TestIncompleteResults(unittest.TestCase):
    def setUp(self):
        with patch("src.session.settings", return_value=SETTINGS):
            self.b = BrowserSession()
        self.b.settings = dict(SETTINGS)
        self.b._log = lambda _m: None
        self.diag = FetchDiagnostics(page_status=200)

    def check(self, got, total, attempt, best):
        return self.b._check_complete_or_retry(cos(got), 1, Facets(total=total), attempt, best, self.diag, 100)

    def test_complete_small_result(self):
        companies, _ = self.check(82, 82, 1, {"companies": [], "pages": 0})
        self.assertEqual(len(companies), 82)

    def test_capped_result_is_complete(self):
        companies, _ = self.check(100, 1621, 1, {"companies": [], "pages": 0})
        self.assertEqual(len(companies), 100)

    def test_small_shortfall_tolerated(self):
        companies, _ = self.check(87, 89, 1, {"companies": [], "pages": 0})
        self.assertEqual(len(companies), 87)

    def test_stall_raises_for_retry_and_remembers_best(self):
        best = {"companies": [], "pages": 0}
        with self.assertRaises(PageNotReadyError) as ctx:
            self.check(40, 89, 1, best)
        self.assertIn("got 40 of 89", str(ctx.exception))
        self.assertIs(ctx.exception.diagnostics, self.diag)  # no VPN rotation for a plain stall
        self.assertEqual(len(best["companies"]), 40)
        with self.assertRaises(PageNotReadyError):
            self.check(30, 246, 2, best)  # expected = 100 (cap)
        self.assertEqual(len(best["companies"]), 40)

    def test_final_attempt_keeps_best(self):
        best = {"companies": cos(70), "pages": 7}
        companies, pages = self.check(40, 89, 3, best)
        self.assertEqual((len(companies), pages), (70, 7))

    def test_requested_page_limit_is_respected(self):
        # caller asked for 3 pages (30 cards) of a 1,621-company search: complete, no retry
        companies, _ = self.b._check_complete_or_retry(
            cos(30), 3, Facets(total=1621), 1, {"companies": [], "pages": 0}, self.diag, 30
        )
        self.assertEqual(len(companies), 30)

    def test_no_counts_means_no_check(self):
        companies, _ = self.b._check_complete_or_retry(cos(5), 1, None, 1, {"companies": [], "pages": 0}, self.diag, 100)
        self.assertEqual(len(companies), 5)


if __name__ == "__main__":
    unittest.main()
