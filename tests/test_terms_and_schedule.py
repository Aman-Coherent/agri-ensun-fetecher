"""Term lists (List 1 / List 2), entity selection, and round-robin order."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.schedule import iter_schedule
from src.terms_loader import (
    load_term_list,
    master_csv_path,
    ordered_terms,
    parse_entity_selector,
)

SAMPLE = """﻿Category,Entity #,Entity,Rank,Search Term
1. Input,1.1,"Seeds, crop protection",1,agri input dealer
1. Input,1.1,"Seeds, crop protection",2,pesticide
1. Input,1.1,"Seeds, crop protection",3,Pesticide
1. Input,1.1,"Seeds, crop protection",4,"2,4-D amine salt 58% SL"
1. Input,1.3,Crop protection,1,pesticide
1. Input,1.3,Crop protection,2,fungicide
2. Infra,2.7,Farm machinery,1,tractor
1. Input,1.10,Ten,1,ten term
"""


def write_sample(text: str = SAMPLE) -> Path:
    tmp = Path(tempfile.mkdtemp()) / "master.csv"
    tmp.write_text(text, encoding="utf-8")
    return tmp


class TestLoad(unittest.TestCase):
    def setUp(self):
        self.tl = load_term_list("list2", path=write_sample())

    def test_entities_in_numeric_order(self):
        self.assertEqual([e.entity_no for e in self.tl.entities], ["1.1", "1.3", "1.10", "2.7"])

    def test_case_duplicate_dropped_within_entity_and_commas_kept(self):
        terms = [t.search_term for t in self.tl.entities[0].terms]
        self.assertEqual(terms, ["agri input dealer", "pesticide", "2,4-D amine salt 58% SL"])
        self.assertEqual(self.tl.entities[0].terms[1].rank, 2)

    def test_shared_term_counted_once_globally(self):
        self.assertEqual(self.tl.term_count, 7)
        self.assertEqual(self.tl.unique_term_count, 6)

    def test_missing_column_rejected(self):
        bad = write_sample("Category,Entity,Search Term\nx,y,z\n")
        with self.assertRaises(ValueError):
            load_term_list("list2", path=bad)


class TestSelector(unittest.TestCase):
    def setUp(self):
        self.tl = load_term_list("list2", path=write_sample())

    def nos(self, sel):
        return [e.entity_no for e in parse_entity_selector(self.tl, sel)]

    def test_all(self):
        self.assertEqual(len(self.nos("all")), 4)
        self.assertEqual(len(self.nos("")), 4)

    def test_category(self):
        self.assertEqual(self.nos("2"), ["2.7"])
        self.assertEqual(self.nos("1"), ["1.1", "1.3", "1.10"])

    def test_entities_and_mixed(self):
        self.assertEqual(self.nos("1.3, 2.7"), ["1.3", "2.7"])
        self.assertEqual(self.nos("2,1.1"), ["1.1", "2.7"])

    def test_unknown_entity(self):
        with self.assertRaises(ValueError):
            parse_entity_selector(self.tl, "9.9")


class TestSchedule(unittest.TestCase):
    def setUp(self):
        self.tl = load_term_list("list2", path=write_sample())

    def test_round_robin(self):
        items = list(iter_schedule(self.tl.entities, "ASIA:India", schedule="round_robin", term_order="csv"))
        order = [(i.entity.entity_no, i.term.search_term) for i in items]
        self.assertEqual(
            order[:4],
            [("1.1", "agri input dealer"), ("1.3", "pesticide"), ("1.10", "ten term"), ("2.7", "tractor")],
        )
        self.assertEqual(order[4], ("1.1", "pesticide"))
        self.assertEqual(len(items), 7)
        self.assertEqual(items[-1].index, 7)
        self.assertEqual(items[0].total, 7)

    def test_sequential(self):
        items = list(iter_schedule(self.tl.entities, "k", schedule="sequential", term_order="csv"))
        self.assertEqual([i.entity.entity_no for i in items[:3]], ["1.1", "1.1", "1.1"])

    def test_terms_per_entity_limit(self):
        items = list(iter_schedule(self.tl.entities, "k", terms_per_entity=1, term_order="csv"))
        self.assertEqual(len(items), 4)

    def test_random_order_is_stable_per_country(self):
        e = self.tl.entities[0]
        a = [t.search_term for t in ordered_terms(e, "ASIA:India", "random")]
        b = [t.search_term for t in ordered_terms(e, "ASIA:India", "random")]
        self.assertEqual(a, b)
        self.assertEqual(sorted(a), sorted(t.search_term for t in e.terms))


class TestRealLists(unittest.TestCase):
    """Read-only checks against the real Agro_Search_Terms folder."""

    def test_both_lists_load(self):
        for list_id, expected_terms in (("list1", 14065), ("list2", 3949)):
            path = master_csv_path(list_id)
            if not path.exists():
                self.skipTest(f"{path} not present on this machine")
            tl = load_term_list(list_id)
            self.assertEqual(len(tl.entities), 52)
            self.assertEqual(len(tl.categories()), 8)
            self.assertEqual(tl.term_count, expected_terms)
            self.assertEqual(tl.entities[0].entity_no, "1.1")
            self.assertEqual(tl.entities[-1].entity_no, "8.3")


if __name__ == "__main__":
    unittest.main()
