"""employee_range is overview band (1-10), not paid employee list count."""

from __future__ import annotations

import unittest

from src.graphql.profile_normalize import parse_employee_range


class TestParseEmployeeRange(unittest.TestCase):
    def test_overview_bands(self) -> None:
        self.assertEqual(parse_employee_range("1-10 Employees"), "1-10")
        self.assertEqual(parse_employee_range("51-100 Employees"), "51-100")
        self.assertEqual(parse_employee_range("10001+ Employees"), "10001+")

    def test_rejects_paid_list_zero(self) -> None:
        self.assertIsNone(parse_employee_range("0 employees"))
        self.assertIsNone(parse_employee_range("0"))
        self.assertIsNone(parse_employee_range(""))

    def test_rejects_bare_counts(self) -> None:
        self.assertIsNone(parse_employee_range("5 employees"))
        self.assertIsNone(parse_employee_range("See 12 employees"))


if __name__ == "__main__":
    unittest.main()
