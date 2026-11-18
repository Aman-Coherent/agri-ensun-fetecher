"""Industry-first split, the missing-gap rule and early stop (scenarios from the UAE/India runs)."""

from __future__ import annotations

import asyncio
import unittest

from src.facets import Facets
from src.split_planner import (
    ORDER_INDUSTRY_FIRST,
    NodeOutcome,
    SearchNode,
    SplitConfig,
    needs_split,
    walk_term,
)

RELATED = ("Agriculture", "Chemicals", "Import and Export", "Wholesale", "Other")
CFG = SplitConfig(
    cap=100, max_depth=2, related_industries=RELATED, order=ORDER_INDUSTRY_FIRST, min_gap=20, done_tolerance=2
)


def keys(a: int, b: int) -> frozenset[str]:
    return frozenset(f"c{i}" for i in range(a, b))


class Runner:
    """Script: (company_type, industry) -> (facets, company keys returned)."""

    def __init__(self, script, counts):
        self.script = script
        self.counts = counts
        self.searched: list[tuple[str, str]] = []
        self.count_calls: list[tuple[str, list[str]]] = []

    async def run(self, node: SearchNode) -> NodeOutcome:
        key = (node.company_type, node.industry)
        self.searched.append(key)
        facets, found = self.script[key]
        return NodeOutcome("completed", len(found), facets, searched=True, company_keys=found)

    def mark_same_as_parent(self, node, parent):
        return NodeOutcome("same_as_parent", parent.result_count, parent.facets, company_keys=parent.company_keys)

    async def industry_counts(self, node, industries):
        self.count_calls.append((node.company_type, list(industries)))
        return {i: self.counts.get((node.company_type, i), 0) for i in industries}


def root() -> SearchNode:
    return SearchNode("United Arab Emirates,null,null", "UAE", "t", "t")


class TestGapRule(unittest.TestCase):
    def test_uae_case_102_vs_100_is_not_split(self):
        # 'agri commodity trading company' x Service provider: count 102, got 100
        o = NodeOutcome("completed", 100, Facets(total=102), company_keys=keys(0, 100))
        self.assertTrue(needs_split(o, 100))  # more than 100 exist...
        self.assertFalse(needs_split(o, 100, min_gap=20))  # ...but only 2 missing: not worth it

    def test_big_gap_splits(self):
        o = NodeOutcome("completed", 100, Facets(total=217), company_keys=keys(0, 100))
        self.assertTrue(needs_split(o, 100, min_gap=20))

    def test_small_gap_term_is_single_search(self):
        runner = Runner({("", ""): (Facets(total=110), keys(0, 100))}, {})
        out = asyncio.run(walk_term(root(), runner, CFG))
        self.assertEqual(runner.searched, [("", "")])
        self.assertEqual(runner.count_calls, [])  # no count check either
        self.assertEqual(out.small_gap_skips, 1)
        self.assertTrue(out.complete)


class TestIndustryFirst(unittest.TestCase):
    def test_industries_then_company_type_only_where_still_big(self):
        script = {
            ("", ""): (
                Facets(total=400, categories=[("MANUFACTURER", 300)], industries=[("Chemicals", 250)]),
                keys(0, 100),
            ),
            # Chemicals 250 > 100 and 150 missing -> split by company type inside it
            ("", "Chemicals"): (
                Facets(total=250, categories=[("MANUFACTURER", 180), ("DISTRIBUTOR", 70)]),
                keys(50, 150),
            ),
            ("MANUFACTURER", "Chemicals"): (Facets(total=180), keys(150, 250)),
            ("DISTRIBUTOR", "Chemicals"): (Facets(total=70), keys(250, 320)),
            ("", "Agriculture"): (Facets(total=60, categories=[("MANUFACTURER", 60)]), keys(320, 380)),
            ("", "Wholesale"): (Facets(total=15), keys(380, 395)),
        }
        counts = {("", "Agriculture"): 60, ("", "Wholesale"): 15}  # others 0
        runner = Runner(script, counts)
        out = asyncio.run(walk_term(root(), runner, CFG))
        # count check skips Chemicals (already shown in ensun's panel)
        self.assertEqual(runner.count_calls, [("", ["Agriculture", "Import and Export", "Wholesale", "Other"])])
        self.assertEqual(
            runner.searched,
            [
                ("", ""),
                ("", "Chemicals"),
                ("MANUFACTURER", "Chemicals"),
                ("DISTRIBUTOR", "Chemicals"),
                ("", "Agriculture"),
                ("", "Wholesale"),
            ],
        )
        self.assertNotIn(("MANUFACTURER", ""), runner.searched)  # no company-type-only search
        self.assertTrue(out.complete)
        self.assertEqual(out.capped_leaves, 1)  # Manufacturer x Chemicals: 180, 100 shown

    def test_early_stop_when_all_counted_companies_found(self):
        script = {
            ("", ""): (Facets(total=130, industries=[("Chemicals", 80)]), keys(0, 100)),
            ("", "Chemicals"): (Facets(total=80), keys(60, 129)),  # now 129 of 130 found
            ("", "Agriculture"): (Facets(total=10), keys(0, 10)),
        }
        runner = Runner(script, {("", "Agriculture"): 10, ("", "Other"): 3})
        stops = []
        out = asyncio.run(
            walk_term(root(), runner, CFG, on_early_stop=lambda n, f, s: stops.append((f, s)))
        )
        self.assertEqual(runner.searched, [("", ""), ("", "Chemicals")])
        self.assertEqual(out.early_stops, 1)
        self.assertEqual(stops, [(129, 2)])  # Agriculture + Other skipped
        self.assertTrue(out.complete)


if __name__ == "__main__":
    unittest.main()
