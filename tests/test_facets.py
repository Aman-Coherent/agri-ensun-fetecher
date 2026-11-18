"""Parsing ensun's filter-panel counts (payload shape captured live 2026-11-11)."""

from __future__ import annotations

import unittest

from src.facets import Facets, facet_request_matches, parse_facet_body

LIVE_BODY = {
    "data": {
        "getExpectedSearchResultCount": {
            "total": 1320,
            "aggregatedByCategory": [
                {"category": "MANUFACTURER", "resultCount": 1320},
                {"category": "SERVICE_PROVIDER", "resultCount": 266},
                {"category": "DISTRIBUTOR", "resultCount": 126},
                {"category": "EDUCATIONAL_INSTITUTION", "resultCount": 5},
            ],
            "aggregatedByCountry": [
                {"location": {"city": None, "state": None, "country": "India"}, "resultCount": 1320}
            ],
            "aggregatedByIndustry": [
                {"industry": {"_id": "665444828e61d1b893ee2005", "name": "Chemicals"}, "resultCount": 539},
                {"industry": {"_id": "x", "name": "Pharmaceuticals"}, "resultCount": 140},
            ],
        }
    }
}
LIVE_VARIABLES = {
    "queryInput": {
        "categories": ["MANUFACTURER"],
        "continents": ["ASIA"],
        "locations": [{"city": None, "country": "India", "state": None}],
        "query": "pesticide",
    }
}


class TestParse(unittest.TestCase):
    def test_parse_live_payload(self):
        f = parse_facet_body(LIVE_BODY)
        self.assertEqual(f.total, 1320)
        self.assertEqual(f.categories[0], ("MANUFACTURER", 1320))
        self.assertEqual(f.industries, [("Chemicals", 539), ("Pharmaceuticals", 140)])
        self.assertEqual(f.category_count("DISTRIBUTOR"), 126)
        self.assertIsNone(f.industry_count("Retail"))

    def test_garbage_returns_none(self):
        for body in (None, {}, {"data": None}, {"data": {"getExpectedSearchResultCount": None}}, "x"):
            self.assertIsNone(parse_facet_body(body))

    def test_zero_total_is_valid(self):
        body = {"data": {"getExpectedSearchResultCount": {"total": 0}}}
        self.assertEqual(parse_facet_body(body).total, 0)

    def test_json_roundtrip(self):
        f = parse_facet_body(LIVE_BODY)
        self.assertEqual(Facets.from_json(f.to_json()), f)
        self.assertIsNone(Facets.from_json(None))
        self.assertIsNone(Facets.from_json("{bad"))


class TestRequestMatching(unittest.TestCase):
    def test_exact_filter_match(self):
        self.assertTrue(facet_request_matches(LIVE_VARIABLES, "pesticide", "MANUFACTURER"))

    def test_case_and_space_insensitive_query(self):
        self.assertTrue(facet_request_matches(LIVE_VARIABLES, "  Pesticide ", "MANUFACTURER"))

    def test_wrong_term_or_filter_rejected(self):
        self.assertFalse(facet_request_matches(LIVE_VARIABLES, "fungicide", "MANUFACTURER"))
        self.assertFalse(facet_request_matches(LIVE_VARIABLES, "pesticide"))  # unfiltered search
        self.assertFalse(facet_request_matches(LIVE_VARIABLES, "pesticide", "DISTRIBUTOR"))
        self.assertFalse(facet_request_matches(LIVE_VARIABLES, "pesticide", "MANUFACTURER", "Chemicals"))

    def test_industry_by_name(self):
        v = {"queryInput": {**LIVE_VARIABLES["queryInput"], "industries": ["Consumer Goods and Services"]}}
        self.assertTrue(
            facet_request_matches(v, "pesticide", "MANUFACTURER", "Consumer Goods and Services")
        )

    def test_malformed_variables(self):
        self.assertFalse(facet_request_matches(None, "pesticide"))
        self.assertFalse(facet_request_matches({"queryInput": "x"}, "pesticide"))


if __name__ == "__main__":
    unittest.main()
