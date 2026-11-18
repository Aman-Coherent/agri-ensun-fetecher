"""Depth-2 split over the project's related-industries list (not just ensun's top 5)."""

from __future__ import annotations

import asyncio
import unittest

import yaml

from src.config_loader import CONFIG_DIR, related_industries
from src.facets import Facets
from src.split_planner import NodeOutcome, SearchNode, SplitConfig, walk_term

# All 60 options of ensun's "Select industry" dropdown, read live 2026-11-11.
ENSUN_INDUSTRIES = {
    "Administration", "Agriculture", "Architecture and Planning", "Automation", "Automotive",
    "Aviation and Aerospace", "Biotechnology", "Building Materials", "Business Services",
    "Business Supplies and Equipment", "Chemicals", "Computer Hardware", "Construction",
    "Consulting", "Consumer Goods and Services", "Cyber Security", "Defense", "Education",
    "Electronics and Electrical engineering", "Environment, Disposal and Recycling",
    "Finance and Insurance", "Food and Beverage", "Gaming", "Glass, Ceramics and Concrete",
    "Healthcare", "Human Resources", "IT, Software and Services", "Import and Export",
    "International Affairs and Trade", "Judiciary", "Logistics, Supply Chain and Transportation",
    "Luxury Goods and Jewelry", "Machinery Manufacturing", "Manufacturing",
    "Maritime and Ship Building", "Marketing Services", "Media and Entertainment", "Medical",
    "Mining", "Nanotechnology", "Oil, Energy and Gas", "Other", "Packaging",
    "Paper and Forest Products", "Pharmaceuticals", "Plastics", "Printing",
    "Railroad Manufacturing", "Real Estate", "Research", "Retail", "Semiconductor", "Sports",
    "Telecommunications", "Textile", "Think Tanks", "Tobacco", "Travel and Tourism",
    "Utilities", "Wholesale",
}
NOT_RELATED = {
    "Gaming", "Judiciary", "Luxury Goods and Jewelry", "Media and Entertainment",
    "Cyber Security", "Defense", "Human Resources", "Marketing Services", "Printing",
    "Railroad Manufacturing", "Real Estate", "Semiconductor", "Sports", "Tobacco",
    "Travel and Tourism", "Business Supplies and Equipment",
}

RELATED = ("Agriculture", "Chemicals", "Pharmaceuticals", "Plastics", "Wholesale", "Other")
CFG = SplitConfig(cap=100, max_depth=2, related_industries=RELATED)

ROOT_FACETS = Facets(total=1621, categories=[("MANUFACTURER", 1320), ("EDUCATIONAL_INSTITUTION", 9)])
# ensun's visible top 5 for Manufacturer — includes an unrelated one (Gaming, for the test)
MFR_FACETS = Facets(
    total=1320,
    categories=ROOT_FACETS.categories,
    industries=[("Chemicals", 539), ("Pharmaceuticals", 140), ("Agriculture", 105), ("Gaming", 60)],
)


class Runner:
    def __init__(self, counts, *, count_error=None):
        self.counts = counts
        self.count_error = count_error
        self.searched: list[tuple[str, str]] = []
        self.count_calls: list[list[str]] = []

    async def run(self, node: SearchNode) -> NodeOutcome:
        key = (node.company_type, node.industry)
        self.searched.append(key)
        facets = {("", ""): ROOT_FACETS, ("MANUFACTURER", ""): MFR_FACETS}.get(key)
        if facets is None:
            facets = Facets(total=node.expected_count or 0)
        return NodeOutcome("completed", min(facets.total, 100), facets, searched=True)

    def mark_same_as_parent(self, node, parent):
        return NodeOutcome("same_as_parent", parent.result_count, parent.facets)

    async def industry_counts(self, node, industries):
        self.count_calls.append(list(industries))
        if self.count_error:
            raise self.count_error
        return {i: self.counts.get(i, 0) for i in industries}


def root() -> SearchNode:
    return SearchNode("India,null,null", "India", "pesticide", "pesticide")


class TestRelatedIndustrySplit(unittest.TestCase):
    def test_searches_related_industries_beyond_top5_and_skips_zero_and_unrelated(self):
        runner = Runner({"Plastics": 27, "Other": 27, "Wholesale": 0})
        out = asyncio.run(walk_term(root(), runner, CFG))
        # only industries ensun did NOT already count are asked for
        self.assertEqual(runner.count_calls, [["Plastics", "Wholesale", "Other"]])
        industries = [ind for ct, ind in runner.searched if ct == "MANUFACTURER" and ind]
        self.assertEqual(industries, ["Chemicals", "Pharmaceuticals", "Agriculture", "Plastics", "Other"])
        self.assertNotIn("Gaming", industries)  # in ensun's top 5 but not related
        self.assertNotIn("Wholesale", industries)  # related but 0 companies
        self.assertTrue(out.complete)
        self.assertEqual(out.capped_leaves, 3)  # Chemicals 539, Pharma 140, Agriculture 105

    def test_count_failure_makes_term_incomplete_without_searching_industries(self):
        runner = Runner({}, count_error=RuntimeError("blocked"))
        errors = []
        out = asyncio.run(walk_term(root(), runner, CFG, on_error=lambda n, e: errors.append(e)))
        self.assertFalse(out.complete)
        self.assertEqual(len(errors), 1)
        self.assertFalse(any(ind for _, ind in runner.searched))

    def test_small_company_type_needs_no_counts(self):
        cfg = SplitConfig(cap=2000, max_depth=2, related_industries=RELATED)
        runner = Runner({})
        asyncio.run(walk_term(root(), runner, cfg))
        self.assertEqual(runner.count_calls, [])


class TestRelatedIndustryConfig(unittest.TestCase):
    def test_config_names_match_ensun_exactly(self):
        names = related_industries()
        self.assertEqual(len(names), len(set(names)))
        unknown = [n for n in names if n not in ENSUN_INDUSTRIES]
        self.assertEqual(unknown, [], "not an ensun industry name (check spelling/commas)")

    def test_config_is_ensun_list_minus_not_related(self):
        self.assertEqual(set(related_industries()), ENSUN_INDUSTRIES - NOT_RELATED)
        self.assertEqual(len(related_industries()), 44)

    def test_file_parses(self):
        with (CONFIG_DIR / "related_industries.yaml").open(encoding="utf-8") as f:
            self.assertIn("related_industries", yaml.safe_load(f))


if __name__ == "__main__":
    unittest.main()
