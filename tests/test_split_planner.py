"""Smart split decisions, using the real numbers seen live on ensun (2026-11-11)."""

from __future__ import annotations

import asyncio
import unittest

from src.facets import Facets
from src.split_planner import (
    NodeOutcome,
    SearchNode,
    SplitConfig,
    child_nodes,
    is_same_as_parent,
    needs_split,
    walk_term,
)

# "top 5" mode: no related list -> industries come from ensun's visible panel
CFG = SplitConfig(
    cap=100,
    max_depth=2,
    fallback_company_types=("MANUFACTURER", "SERVICE_PROVIDER"),
)

# 'pesticide' in India, captured live
PESTICIDE_ROOT = Facets(
    total=1621,
    categories=[
        ("MANUFACTURER", 1320),
        ("SERVICE_PROVIDER", 473),
        ("DISTRIBUTOR", 182),
        ("EDUCATIONAL_INSTITUTION", 9),
    ],
    industries=[
        ("Chemicals", 606),
        ("Consumer Goods and Services", 159),
        ("Pharmaceuticals", 153),
        ("Agriculture", 126),
        ("Biotechnology", 64),
    ],
)
PESTICIDE_MFR = Facets(
    total=1320,
    categories=PESTICIDE_ROOT.categories,
    industries=[
        ("Chemicals", 539),
        ("Pharmaceuticals", 140),
        ("Agriculture", 105),
        ("Consumer Goods and Services", 89),
        ("Biotechnology", 55),
    ],
)


def root(term: str = "pesticide") -> SearchNode:
    return SearchNode(
        location_triple="India,null,null",
        location_label="India",
        term_key=term,
        search_term=term,
    )


class FakeRunner:
    """Returns scripted facets per (company_type, industry); records searches."""

    def __init__(self, facets_by_filter, *, fail=(), stored=None):
        self.facets_by_filter = facets_by_filter
        self.fail = set(fail)
        self.stored = stored or {}
        self.searched: list[tuple[str, str]] = []
        self.same: list[tuple[str, str]] = []

    async def run(self, node: SearchNode) -> NodeOutcome:
        key = (node.company_type, node.industry)
        if key in self.stored:
            return self.stored[key]
        self.searched.append(key)
        if key in self.fail:
            return NodeOutcome("failed", 0, None, searched=True)
        facets = self.facets_by_filter.get(key)
        if facets is None and key in self.facets_by_filter:
            return NodeOutcome("completed", 100, None, searched=True)
        if facets is None:
            # unscripted leaf: count = expected, results = min(count, 100)
            total = node.expected_count or 0
            facets = Facets(total=total)
        n = min(facets.total, 100)
        return NodeOutcome("completed" if n else "empty", n, facets, searched=True)

    def mark_same_as_parent(self, node: SearchNode, parent: NodeOutcome) -> NodeOutcome:
        self.same.append((node.company_type, node.industry))
        return NodeOutcome("same_as_parent", parent.result_count, parent.facets)

    async def industry_counts(self, node: SearchNode, industries: list[str]) -> dict[str, int]:
        raise AssertionError("top-5 mode must not ask for counts")


def run(coro):
    return asyncio.run(coro)


class TestNeedsSplit(unittest.TestCase):
    def test_small_term_is_one_search(self):
        o = NodeOutcome("completed", 40, Facets(total=40))
        self.assertFalse(needs_split(o, 100))

    def test_exactly_cap_is_complete(self):
        self.assertFalse(needs_split(NodeOutcome("completed", 100, Facets(total=100)), 100))

    def test_over_cap_splits(self):
        self.assertTrue(needs_split(NodeOutcome("completed", 100, Facets(total=101)), 100))

    def test_stalled_pagination_still_splits_when_count_says_so(self):
        # pagination stalled at 59 cards, but ensun says 1,621 exist
        self.assertTrue(needs_split(NodeOutcome("completed", 59, Facets(total=1621)), 100))

    def test_no_counts_falls_back_to_full_page_signal(self):
        self.assertTrue(needs_split(NodeOutcome("completed", 100, None), 100))
        self.assertFalse(needs_split(NodeOutcome("completed", 37, None), 100))

    def test_failed_never_splits(self):
        self.assertFalse(needs_split(NodeOutcome("failed", 0, Facets(total=5000)), 100))


class TestChildNodes(unittest.TestCase):
    def test_root_children_are_company_types_largest_first_skipping_zero(self):
        facets = Facets(total=500, categories=[("DISTRIBUTOR", 50), ("MANUFACTURER", 400), ("X", 0)])
        kids = child_nodes(root(), NodeOutcome("completed", 100, facets), CFG)
        self.assertEqual([k.company_type for k in kids], ["MANUFACTURER", "DISTRIBUTOR"])
        self.assertEqual([k.expected_count for k in kids], [400, 50])
        self.assertTrue(all(k.depth == 1 and k.industry == "" for k in kids))
        self.assertEqual(kids[0].parent_id, root().node_id)

    def test_depth1_children_are_industries_within_type(self):
        mfr = child_nodes(root(), NodeOutcome("completed", 100, PESTICIDE_ROOT), CFG)[0]
        kids = child_nodes(mfr, NodeOutcome("completed", 100, PESTICIDE_MFR), CFG)
        self.assertEqual(kids[0].industry, "Chemicals")
        self.assertEqual(kids[0].expected_count, 539)
        self.assertTrue(all(k.company_type == "MANUFACTURER" and k.depth == 2 for k in kids))

    def test_max_depth_stops(self):
        leaf = SearchNode("India,null,null", "India", "t", "t", "MANUFACTURER", "Chemicals", depth=2)
        self.assertEqual(child_nodes(leaf, NodeOutcome("completed", 100, Facets(total=539)), CFG), [])

    def test_fallback_company_types_when_counts_missing(self):
        kids = child_nodes(root(), NodeOutcome("completed", 100, None), CFG)
        self.assertEqual([k.company_type for k in kids], ["MANUFACTURER", "SERVICE_PROVIDER"])
        self.assertTrue(all(k.expected_count is None for k in kids))

    def test_node_ids_distinct_per_filter(self):
        a = SearchNode("India,null,null", "India", "t", "t")
        b = SearchNode("India,null,null", "India", "t", "t", "MANUFACTURER")
        c = SearchNode("India,null,null", "India", "t", "t", "MANUFACTURER", "Chemicals")
        self.assertEqual(len({a.node_id, b.node_id, c.node_id}), 3)


class TestWalkTerm(unittest.TestCase):
    def test_specific_term_needs_one_search(self):
        runner = FakeRunner({("", ""): Facets(total=40, categories=[("MANUFACTURER", 40)])})
        out = run(walk_term(root("imidacloprid 17.8% SL"), runner, CFG))
        self.assertEqual(runner.searched, [("", "")])
        self.assertEqual(out.searches_run, 1)
        self.assertTrue(out.complete)
        self.assertEqual(out.capped_leaves, 0)

    def test_pesticide_india_tree(self):
        runner = FakeRunner(
            {
                ("", ""): PESTICIDE_ROOT,
                ("MANUFACTURER", ""): PESTICIDE_MFR,
                ("SERVICE_PROVIDER", ""): Facets(total=473, industries=[("Chemicals", 200), ("Other", 90)]),
                ("DISTRIBUTOR", ""): Facets(total=182, industries=[("Agriculture", 120), ("Chemicals", 30)]),
            }
        )
        out = run(walk_term(root(), runner, CFG))
        self.assertTrue(out.complete)
        # depth 0 + 4 company types + 5 (MFR) + 2 (SP) + 2 (DIST) industries
        self.assertEqual(out.searches_run, 1 + 4 + 5 + 2 + 2)
        self.assertEqual(runner.searched[0], ("", ""))
        self.assertEqual(runner.searched[1], ("MANUFACTURER", ""))
        self.assertEqual(runner.searched[2], ("MANUFACTURER", "Chemicals"))  # depth-first
        self.assertNotIn(("EDUCATIONAL_INSTITUTION", "Chemicals"), runner.searched)  # 9 <= 100
        # capped at the deepest level: MFR x Chemicals (539), MFR x Pharma (140),
        # MFR x Agriculture (105), SP x Chemicals (200), DIST x Agriculture (120)
        self.assertEqual(out.capped_leaves, 5)

    def test_same_as_parent_skips_search_but_still_splits(self):
        only_mfr = Facets(
            total=300,
            categories=[("MANUFACTURER", 300)],
            industries=[("Agriculture", 250), ("Chemicals", 40)],
        )
        runner = FakeRunner({("", ""): only_mfr})
        out = run(walk_term(root("drip kit"), runner, CFG))
        self.assertEqual(runner.same, [("MANUFACTURER", "")])
        self.assertNotIn(("MANUFACTURER", ""), runner.searched)
        # industries come from the parent's counts (identical result set)
        self.assertIn(("MANUFACTURER", "Agriculture"), runner.searched)
        self.assertIn(("MANUFACTURER", "Chemicals"), runner.searched)
        self.assertEqual(out.same_as_parent, 1)
        self.assertEqual(out.searches_run, 3)

    def test_same_as_parent_detection(self):
        node = SearchNode("India,null,null", "India", "t", "t", "MANUFACTURER", expected_count=300)
        self.assertTrue(is_same_as_parent(node, NodeOutcome("completed", 100, Facets(total=300))))
        self.assertFalse(is_same_as_parent(node, NodeOutcome("completed", 100, Facets(total=301))))
        self.assertFalse(is_same_as_parent(node, NodeOutcome("completed", 100, None)))

    def test_failed_node_makes_term_incomplete_but_siblings_continue(self):
        runner = FakeRunner(
            {("", ""): PESTICIDE_ROOT, ("MANUFACTURER", ""): PESTICIDE_MFR},
            fail={("MANUFACTURER", "")},
        )
        out = run(walk_term(root(), runner, CFG))
        self.assertFalse(out.complete)
        self.assertEqual(out.failed, 1)
        self.assertIn(("SERVICE_PROVIDER", ""), runner.searched)
        self.assertNotIn(("MANUFACTURER", "Chemicals"), runner.searched)

    def test_resume_reuses_stored_nodes(self):
        stored = {
            ("", ""): NodeOutcome("completed", 100, PESTICIDE_ROOT),
            ("MANUFACTURER", ""): NodeOutcome("completed", 100, PESTICIDE_MFR),
        }
        runner = FakeRunner({}, stored=stored)
        out = run(walk_term(root(), runner, CFG))
        self.assertNotIn(("", ""), runner.searched)
        self.assertNotIn(("MANUFACTURER", ""), runner.searched)
        self.assertEqual(out.reused, 2)
        self.assertIn(("MANUFACTURER", "Chemicals"), runner.searched)

    def test_stop_request_marks_incomplete(self):
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 1  # allow only the root

        runner = FakeRunner({("", ""): PESTICIDE_ROOT})
        out = run(walk_term(root(), runner, CFG, should_stop=stop))
        self.assertEqual(runner.searched, [("", "")])
        self.assertFalse(out.complete)

    def test_max_depth_one_only_splits_by_company_type(self):
        cfg = SplitConfig(cap=100, max_depth=1)
        runner = FakeRunner({("", ""): PESTICIDE_ROOT, ("MANUFACTURER", ""): PESTICIDE_MFR})
        out = run(walk_term(root(), runner, cfg))
        self.assertEqual(out.searches_run, 5)
        self.assertTrue(all(ind == "" for _, ind in runner.searched))


if __name__ == "__main__":
    unittest.main()
