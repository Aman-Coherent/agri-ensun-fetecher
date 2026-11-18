"""End-to-end search phase with a scripted fake browser (no network).

Covers: round-robin order, a term shared by two entities searched once, smart
split depth 0/1/2, atomic saves, resume (2nd run = 0 searches), failed-node
retry on the next run, and the one-row-per-entity export with merged terms.
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.browser_errors import BlockedError
from src.facets import Facets
from src.geo import Continent, Country
from src.search_phase import SearchPhaseRunner, SearchRunConfig
from src.storage.country_db import CountryDatabase
from src.storage.exports import build_entity_rows, export_entity_search_csv
from src.terms_loader import load_term_list

TERMS_CSV = """Category,Entity #,Entity,Rank,Search Term
1. Input & upstream ecosystem,1.1,Seeds supplier,1,agri input dealer
1. Input & upstream ecosystem,1.1,Seeds supplier,2,pesticide
1. Input & upstream ecosystem,1.3,Crop protection supplier,1,Pesticide
1. Input & upstream ecosystem,1.3,Crop protection supplier,2,fungicide
2. Infrastructure,2.7,Farm machinery OEMs,1,tractor
"""


def co(i: int) -> dict:
    return {
        "company_id": f"{i:024x}",
        "company_name": f"Company {i}",
        "city": "Pune",
        "country": "Testland",
        "grade": "A",
        "employee_range": "11-50",
        "founding_year": "2001",
        "key_takeaway": f"takeaway {i}",
        "reference_type": "Product",
        "reference_title": None,
        "reference_snippet": None,
        "reference_url": None,
        "logo_url": None,
        "profile_url": None,
        "raw": {"name": f"Company {i}", "text": "card text", "lines": ["card", "text"]},
    }


def cos(start: int, end: int) -> list[dict]:
    return [co(i) for i in range(start, end)]


# ensun count per (term, company type, industry); anything missing = 0 companies
COUNTS = {("pesticide", "", "Plastics"): 7}

# Production settings: two passes, industry first, min_gap 20.
SCRIPT = {
    ("agri input dealer", "", ""): ([], 0, Facets(total=0)),
    # 160 counted, 100 found -> 60 missing -> Pass 2 (industry split)
    ("pesticide", "", ""): (
        cos(0, 100),
        10,
        Facets(total=160, categories=[("MANUFACTURER", 130), ("DISTRIBUTOR", 30)],
               industries=[("Chemicals", 90), ("Agriculture", 63)]),
    ),
    ("pesticide", "", "Chemicals"): (cos(0, 90), 9, Facets(total=90, categories=[("MANUFACTURER", 90)])),
    ("pesticide", "", "Agriculture"): (cos(90, 153), 7, Facets(total=63)),
    # related industry NOT in ensun's visible top 5 -> found via the count check
    ("pesticide", "", "Plastics"): (cos(153, 160), 1, Facets(total=7)),
    # 110 counted, 100 found -> only 10 missing -> not worth a split (min_gap 20)
    ("fungicide", "", ""): ([co(0)] + cos(201, 300), 10, Facets(total=110)),
    ("tractor", "", ""): ([co(300)], 1, Facets(total=1)),
}


class FakeBrowser:
    def __init__(self, fail_keys=()):
        self.calls: list[tuple[str, str, str]] = []
        self.count_calls: list[tuple[str, str, int]] = []
        self.fail_keys = set(fail_keys)

    def set_scrape_id(self, *_a):
        pass

    def set_status_reporter(self, *_a):
        pass

    def set_log_callback(self, *_a):
        pass

    async def start(self, headless=True):
        pass

    async def stop(self):
        pass

    async def close_search_session(self, force_cookie_save=False):
        pass

    async def warm_up_cookies(self):
        return True

    async def count_results(self, term, continent, location, company_type, industries):
        self.count_calls.append((term.lower(), company_type, len(industries)))
        return {i: COUNTS.get((term.lower(), company_type, i), 0) for i in industries}

    async def fetch_search_pages(self, term, continent, location, company_type="", industry="", max_pages=None):
        key = (term.lower(), company_type, industry)
        self.calls.append(key)
        assert continent == "ASIA" and location.startswith("Testland")
        if key in self.fail_keys:
            raise BlockedError("blocked: scripted failure after retries")
        companies, pages, facets = SCRIPT[key]
        return [dict(c) for c in companies], pages, facets


class DummyReporter:
    def __init__(self, *_a, **_k):
        pass

    def set_message_provider(self, *_a):
        pass

    async def start(self):
        pass

    async def stop(self, *_a):
        pass


async def _no_sleep(*_a, **_k):
    return None


def SearchNodeFor(term: str, company_type: str = "", industry: str = ""):
    from src.split_planner import SearchNode

    return SearchNode("Testland,null,null", "x", term, term, company_type, industry)


class SearchFlowTest(unittest.TestCase):
    def setUp(self):
        path = Path(tempfile.mkdtemp()) / "master.csv"
        path.write_text(TERMS_CSV, encoding="utf-8")
        self.term_list = load_term_list("list2", path=path)

    def make_runner(self, country_name: str, browser: FakeBrowser, **cfg) -> SearchPhaseRunner:
        country = Country(name=country_name, continent_code="ASIA", ensun_location="Testland,null,null")
        continent = Continent(code="ASIA", ensun_code="ASIA", countries=[country])
        with patch("src.search_phase.BrowserSession", return_value=browser), patch(
            "src.search_phase.ScraperStatusReporter", DummyReporter
        ):
            return SearchPhaseRunner(
                SearchRunConfig(continent=continent, country=country, list_id="list2", **cfg),
                term_list=self.term_list,
            )

    def run_runner(self, runner: SearchPhaseRunner) -> None:
        with patch("src.search_phase.interruptible_sleep", _no_sleep), patch(
            "src.search_phase.register_shutdown_handler", lambda *_a: None
        ), patch("src.search_phase.SearchPhaseRunner._log", lambda self, msg, **_k: None):
            runner.run()

    def test_full_flow_resume_and_export(self):
        browser = FakeBrowser()
        self.run_runner(self.make_runner("Flowland", browser))

        # Pass 1 (round robin, no filters): 1.1 'agri input dealer', 1.3 'Pesticide'
        # (60 missing -> queued), 2.7 'tractor', 1.1 'pesticide' (same term, already
        # queued), 1.3 'fungicide' (only 10 missing -> done, no split).
        # Pass 2: Pesticide by industry, largest first.
        self.assertEqual(
            browser.calls,
            [
                ("agri input dealer", "", ""),
                ("pesticide", "", ""),
                ("tractor", "", ""),
                ("fungicide", "", ""),
                ("pesticide", "", "Chemicals"),
                ("pesticide", "", "Agriculture"),
                ("pesticide", "", "Plastics"),
            ],
        )
        # one count check: 44 related industries minus the 2 ensun already showed
        self.assertEqual(browser.count_calls, [("pesticide", "", 42)])

        db = CountryDatabase("ASIA", "Flowland")
        try:
            stats = db.search_stats()
            self.assertEqual(stats["terms_done"], 4)
            self.assertEqual(stats["searches_done"], 7)
            self.assertEqual(db.unique_company_count(), 260)  # 0..159, 201..299, 300

            rows = build_entity_rows(db)
            by = {(r["entity_no"], r["company_name"]): r for r in rows}
            # pesticide companies credited to BOTH entities listing the term
            self.assertIn(("1.1", "Company 5"), by)
            self.assertIn(("1.3", "Company 5"), by)
            # same company, same entity, two terms -> ONE row, terms merged
            c0 = by[("1.3", "Company 0")]
            self.assertEqual(c0["search_terms"], "Pesticide | fungicide")
            self.assertEqual(c0["search_term_count"], 2)
            self.assertEqual(c0["industries"], "Chemicals")
            self.assertEqual(by[("1.1", "Company 0")]["search_terms"], "pesticide")
            self.assertEqual(by[("1.3", "Company 250")]["search_terms"], "fungicide")
            self.assertEqual(by[("1.3", "Company 120")]["industries"], "Agriculture")
            self.assertEqual(by[("1.3", "Company 155")]["industries"], "Plastics")
            self.assertEqual(len([r for r in rows if r["entity_no"] == "1.1"]), 160)
            self.assertEqual(len([r for r in rows if r["entity_no"] == "1.3"]), 259)
            self.assertEqual(len([r for r in rows if r["entity_no"] == "2.7"]), 1)
            self.assertEqual(len(rows), 420)
            self.assertEqual(rows[0]["entity_no"], "1.1")
            self.assertTrue(by[("1.1", "Company 5")]["profile_url"].startswith("https://ensun.io/company/"))

            path = export_entity_search_csv(db)
            with path.open(encoding="utf-8-sig", newline="") as f:
                exported = list(csv.DictReader(f))
            self.assertEqual(len(exported), 420)
            self.assertEqual(exported[0]["entity_no"], "1.1")
        finally:
            db.close()

        # Resume: nothing left to search
        browser2 = FakeBrowser()
        self.run_runner(self.make_runner("Flowland", browser2))
        self.assertEqual(browser2.calls, [])
        self.assertEqual(browser2.count_calls, [])  # counts were stored

    def test_failed_search_retried_next_run_only(self):
        fail = ("pesticide", "", "Agriculture")
        browser = FakeBrowser(fail_keys=[fail])
        self.run_runner(self.make_runner("Failland", browser))
        # pesticide searched once (shared by 1.1 and 1.3)
        self.assertEqual(browser.calls.count(("pesticide", "", "")), 1)
        self.assertEqual(browser.calls.count(fail), 1)

        db = CountryDatabase("ASIA", "Failland")
        try:
            done = db.load_done_term_keys("Testland,null,null")
            self.assertNotIn("pesticide", done)
            self.assertIn("fungicide", done)
            self.assertEqual(db.search_stats().get("failed"), 1)
        finally:
            db.close()

        browser2 = FakeBrowser()
        self.run_runner(self.make_runner("Failland", browser2))
        self.assertEqual(browser2.calls, [fail])  # only the failed node is redone
        self.assertEqual(browser2.count_calls, [])  # stored industry counts reused
        db = CountryDatabase("ASIA", "Failland")
        try:
            self.assertIn("pesticide", db.load_done_term_keys("Testland,null,null"))
            self.assertEqual(db.search_stats().get("failed", 0), 0)
        finally:
            db.close()

    def test_saved_short_result_is_searched_again_once(self):
        from src.split_planner import SearchNode

        def short_node(term: str, runs: int) -> None:
            node = SearchNode("Testland,null,null", "Shortland", term, term)
            for _ in range(runs):
                db.mark_node_in_progress(node)
            db.save_node_results(node, [co(i) for i in range(10)], 1, Facets(total=50))

        db = CountryDatabase("ASIA", "Shortland")
        try:
            short_node("fungicide", runs=1)  # stalled once -> retry
            short_node("tractor", runs=2)  # already retried -> accept
        finally:
            db.close()
        browser = FakeBrowser()
        self.run_runner(self.make_runner("Shortland", browser, entity_selector="1.3,2.7"))
        self.assertIn(("fungicide", "", ""), browser.calls)
        self.assertNotIn(("tractor", "", ""), browser.calls)

    def test_ctrl_c_mid_search_then_resume(self):
        """Ctrl+C while a search is running: clean stop, nothing half-saved, resume redoes only that search."""
        import asyncio as aio

        from src import shutdown

        interrupt_at = ("pesticide", "", "Chemicals")  # first Pass-2 search

        class CtrlCBrowser(FakeBrowser):
            async def fetch_search_pages(self, term, continent, location, company_type="", industry="", max_pages=None):
                if (term.lower(), company_type, industry) == interrupt_at:
                    self.calls.append(interrupt_at)
                    shutdown.request_shutdown()  # exactly what pressing Ctrl+C triggers
                    await aio.sleep(30)  # the "search" is cancelled while waiting
                return await super().fetch_search_pages(term, continent, location, company_type, industry, max_pages)

        browser = CtrlCBrowser()
        try:
            self.run_runner(self.make_runner("Ctrlcland", browser))
        finally:
            shutdown._shutdown_requested = False  # module-global; reset for other tests
        self.assertEqual(browser.calls[-1], interrupt_at)  # stopped right there
        self.assertNotIn(("pesticide", "", "Agriculture"), browser.calls)  # nothing ran after Ctrl+C

        db = CountryDatabase("ASIA", "Ctrlcland")
        try:
            done = db.load_done_term_keys("Testland,null,null")
            # Pass 1 finished these; 'pesticide' was mid-split in Pass 2: not done
            self.assertEqual(done, {"agri input dealer", "tractor", "fungicide"})
            node = SearchNodeFor("pesticide", "", "Chemicals")
            self.assertEqual(db.get_node(node.node_id)["status"], "in_progress")
            rows = db.conn.execute(
                "SELECT COUNT(*) FROM search_results WHERE node_id = ?", (node.node_id,)
            ).fetchone()[0]
            self.assertEqual(rows, 0)  # half-finished search saved nothing
            # searches finished before Ctrl+C were saved
            self.assertGreater(db.search_stats()["searches_done"], 0)
        finally:
            db.close()
        from src.config_loader import OUTPUT_DIR

        self.assertTrue((OUTPUT_DIR / "ASIA" / "ctrlcland" / "ctrlcland_search_by_entity.csv").exists())

        # Run again: only the interrupted search + what never started
        browser2 = FakeBrowser()
        self.run_runner(self.make_runner("Ctrlcland", browser2))
        self.assertEqual(browser2.calls[0], interrupt_at)
        self.assertNotIn(("agri input dealer", "", ""), browser2.calls)
        self.assertNotIn(("pesticide", "", ""), browser2.calls)
        self.assertNotIn(("tractor", "", ""), browser2.calls)
        db = CountryDatabase("ASIA", "Ctrlcland")
        try:
            self.assertEqual(db.search_stats()["terms_done"], 4)
            self.assertEqual(db.search_stats().get("in_progress", 0), 0)
        finally:
            db.close()

    def test_entity_selection_and_max_searches(self):
        browser = FakeBrowser()
        self.run_runner(self.make_runner("Pilotland", browser, entity_selector="2.7"))
        self.assertEqual(browser.calls, [("tractor", "", "")])

        browser2 = FakeBrowser()
        self.run_runner(self.make_runner("Limitland", browser2, max_searches=3))
        self.assertEqual(len(browser2.calls), 3)
        db = CountryDatabase("ASIA", "Limitland")
        try:
            # 'Pesticide' stopped mid-split -> not marked done
            self.assertNotIn("pesticide", db.load_done_term_keys("Testland,null,null"))
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
