"""Search-side database behaviour (runs in the temp runtime dir from conftest)."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from src.facets import Facets
from src.split_planner import SearchNode
from src.storage.country_db import CountryDatabase
from src.storage.exports import export_entity_full_csv, export_entity_search_csv
from src.terms_loader import load_term_list

LOC = "Dbland,null,null"


def node(term="pesticide", ct="", ind="", depth=0, expected=None) -> SearchNode:
    return SearchNode(LOC, "Dbland", term.lower(), term, ct, ind, depth=depth, expected_count=expected)


def company(i: int) -> dict:
    return {
        "company_id": f"{i:024x}",
        "company_name": f"Co {i}",
        "employee_range": "1-10",
        "raw": {"name": f"Co {i}", "text": "a\nb", "lines": ["a", "b"]},
    }


class CountryDbSearchTest(unittest.TestCase):
    def setUp(self):
        self.name = f"Dbland{id(self)}"
        self.db = CountryDatabase("ASIA", self.name)

    def tearDown(self):
        self.db.close()

    def test_resave_replaces_rows_not_duplicates(self):
        n = node()
        self.db.mark_node_in_progress(n)
        self.db.save_node_results(n, [company(1), company(2)], 1, Facets(total=2))
        self.db.save_node_results(n, [company(1), company(2), company(3)], 1, Facets(total=3))
        rows = self.db.conn.execute(
            "SELECT COUNT(*) FROM search_results WHERE node_id = ?", (n.node_id,)
        ).fetchone()[0]
        self.assertEqual(rows, 3)
        got = self.db.get_node(n.node_id)
        self.assertEqual(got["status"], "completed")
        self.assertEqual(got["result_count"], 3)
        self.assertEqual(got["attempts"], 1)
        self.assertEqual(Facets.from_json(got["facets_json"]).total, 3)
        self.assertEqual(self.db.unique_company_count(), 3)

    def test_empty_search_status(self):
        n = node("nothing here")
        self.assertEqual(self.db.save_node_results(n, [], 0, Facets(total=0)), "empty")

    def test_raw_json_drops_duplicate_lines(self):
        n = node()
        self.db.save_node_results(n, [company(1)], 1, None)
        raw = json.loads(self.db.conn.execute("SELECT raw_json FROM search_results").fetchone()[0])
        self.assertNotIn("lines", raw)
        self.assertEqual(raw["text"], "a\nb")

    def test_same_as_parent_never_overwrites_a_real_search(self):
        n = node(ct="MANUFACTURER", depth=1, expected=50)
        self.db.save_node_results(n, [company(1)], 1, Facets(total=50))
        self.db.mark_node_same_as_parent(n, Facets(total=50), 50)
        self.assertEqual(self.db.get_node(n.node_id)["status"], "completed")
        m = node(ct="DISTRIBUTOR", depth=1, expected=50)
        self.db.mark_node_same_as_parent(m, Facets(total=50), 42)
        row = self.db.get_node(m.node_id)
        self.assertEqual(row["status"], "same_as_parent")
        self.assertEqual(row["result_count"], 42)

    def test_crash_leftovers_reset(self):
        n = node()
        self.db.mark_node_in_progress(n)
        self.assertEqual(self.db.reset_in_progress_nodes(), 1)
        self.assertEqual(self.db.get_node(n.node_id)["status"], "pending")

    def test_failed_then_success(self):
        n = node()
        self.db.mark_node_in_progress(n)
        self.db.mark_node_failed(n, "blocked")
        self.assertEqual(self.db.search_stats()["failed"], 1)
        self.db.mark_node_in_progress(n)
        self.db.save_node_results(n, [company(9)], 1, Facets(total=1))
        row = self.db.get_node(n.node_id)
        self.assertEqual((row["status"], row["attempts"], row["error_message"]), ("completed", 2, None))

    def test_term_done_and_force_clear(self):
        n = node()
        self.db.save_node_results(n, [company(1), company(2)], 1, Facets(total=2))
        unique = self.db.mark_term_done(LOC, "pesticide", "pesticide", nodes_total=1, searches_run=1, capped_leaves=0)
        self.assertEqual(unique, 2)
        self.assertEqual(self.db.load_done_term_keys(LOC), {"pesticide"})
        self.assertEqual(self.db.load_done_term_keys("Other,null,null"), set())
        self.db.clear_term_done(LOC, ["pesticide"])
        self.assertEqual(self.db.load_done_term_keys(LOC), set())

    def test_comma_term_survives_export_and_full_export_without_profiles(self):
        csv_path = Path(tempfile.mkdtemp()) / "m.csv"
        csv_path.write_text(
            "Category,Entity #,Entity,Rank,Search Term\n"
            'c,1.1,E,1,"2,4-D amine salt 58% SL"\n'
            "c,1.1,E,2,herbicide\n",
            encoding="utf-8",
        )
        tl = load_term_list("list2", path=csv_path)
        self.db.save_term_entities(t for e in tl.entities for t in e.terms)
        for t in tl.entities[0].terms:
            self.db.save_node_results(node(t.search_term), [company(1)], 1, Facets(total=1))
        path = export_entity_search_csv(self.db)
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["search_terms"], "2,4-D amine salt 58% SL | herbicide")
        self.assertEqual(rows[0]["employee_range"], "\t1-10 Employees")

        full = export_entity_full_csv(self.db)
        with full.open(encoding="utf-8-sig", newline="") as f:
            frows = list(csv.DictReader(f))
        self.assertEqual(frows[0]["website_url"], "")
        self.assertIn("profile_status", frows[0])


if __name__ == "__main__":
    unittest.main()
