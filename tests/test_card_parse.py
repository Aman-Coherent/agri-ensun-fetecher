"""Search-card text parsing for both ensun layouts seen in production."""

from __future__ import annotations

import unittest

from src.graphql.extract import parse_card_lines

# Live card, 2026-11-11 (q=pesticide, India). No "Key takeaway" label anymore.
NOV_TEXT = (
    "Willowood\n\nNew Delhi, India\n\nD\n\n1001-5000 Employees\n\n1992\n\nWebsite\n\n"
    "Willowood India is a global producer and distributor of pesticides and crop protection "
    "chemicals, offering insecticides, herbicides, fungicides, bio-pesticides, and growth "
    "promoters across 50+ countries. Source >\n\nCriteria match\n\nFind 389 employees >\n\n"
    "Lookalike companies >\n\nSave\n\nPhone\n\nMail"
)

# Stored card from the nutraceutical scraper, July 2026 (labelled layout).
JUL_TEXT = (
    "Air Products UKI\n\nWalton-on-Thames, United Kingdom\n\nA\n\n10001+ Employees\n\n1940\n\n"
    "Key takeaway\n\nAir Products specializes in providing pharmaceutical-grade gases, including "
    "nitrogen and oxygen.\n\nReference\n\nFind employees\n\nSimilar companies\n\nProduct\n\n"
    "Pharma Grade Gases"
)


def lines_of(text: str) -> list[str]:
    return [s.strip() for s in text.split("\n") if s.strip()]


class TestCardParse(unittest.TestCase):
    def test_november_layout(self):
        f = parse_card_lines("Willowood", NOV_TEXT, lines_of(NOV_TEXT))
        self.assertEqual((f["city"], f["country"]), ("New Delhi", "India"))
        self.assertEqual(f["grade"], "D")
        self.assertEqual(f["employee_range"], "1001-5000")
        self.assertEqual(f["founding_year"], "1992")
        self.assertTrue(f["key_takeaway"].startswith("Willowood India is a global producer"))
        self.assertTrue(f["key_takeaway"].endswith("50+ countries."))

    def test_july_layout_unchanged(self):
        f = parse_card_lines("Air Products UKI", JUL_TEXT, lines_of(JUL_TEXT))
        self.assertEqual((f["city"], f["country"]), ("Walton-on-Thames", "United Kingdom"))
        self.assertEqual((f["grade"], f["employee_range"], f["founding_year"]), ("A", "10001+", "1940"))
        self.assertTrue(f["key_takeaway"].startswith("Air Products specializes"))
        self.assertEqual((f["reference_type"], f["reference_title"]), ("Product", "Pharma Grade Gases"))

    def test_comma_description_does_not_overwrite_location(self):
        text = "Acme\n\nPune, India\n\nAcme makes drip kits, sprinklers, valves. Source >"
        f = parse_card_lines("Acme", text, lines_of(text))
        self.assertEqual((f["city"], f["country"]), ("Pune", "India"))

    def test_empty_card(self):
        f = parse_card_lines("X", "", [])
        self.assertIsNone(f["city"])
        self.assertIsNone(f["key_takeaway"])


if __name__ == "__main__":
    unittest.main()
