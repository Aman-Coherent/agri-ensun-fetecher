"""Fast pagination: page 1 from the records behind the cards, pages 2..N requested directly."""

from __future__ import annotations

import asyncio
import json
import re
import unittest
from unittest.mock import patch

from src.browser_errors import BlockedError
from src.facets import Facets
from src.graphql import extract as extract_mod
from src.session import _EMBEDDED_DATA_JS, _FETCH_RSC_JS, _PAGE1_RECORDS_JS, BrowserSession


def record(i: int) -> dict:
    cid = f"{i:024x}"
    return {
        "company": {"__typename": "CrawledCompanyOverview", "_id": cid, "name": f"Co {i}",
                    "location": {"city": "Dubai", "country": "United Arab Emirates"},
                    "websiteUrl": {"target": f"https://co{i}.example"}},
        "pageHit": {"takeaway": f"takeaway {i}"},
    }


def rsc_text(records: list[dict]) -> str:
    return '0:["$","$L1",null,{"x":1}]\n1:["$","$L2",null,{"results":' + json.dumps(records) + "}]"


class FakePage:
    def __init__(self, total: int, *, page1_from_props=True, statuses=None, dom_cards=True):
        self.records = [record(i) for i in range(total)]
        self.page1_from_props = page1_from_props
        self.statuses = statuses or {}
        self.dom_cards = dom_cards
        self.requested: list[int] = []

    async def wait_for_timeout(self, _ms):
        return None

    async def evaluate(self, js, arg=None):
        if js == _PAGE1_RECORDS_JS:
            return self.records[:10] if self.page1_from_props else None
        if js == _EMBEDDED_DATA_JS:
            return ""
        if js == _FETCH_RSC_JS:
            n = int(re.search(r"[?&]page=(\d+)", arg).group(1))
            self.requested.append(n)
            status = self.statuses.get(n, 200)
            chunk = self.records[(n - 1) * 10 : n * 10] if status == 200 else []
            return {"status": status, "text": rsc_text(chunk) if status == 200 else "Forbidden"}
        if js == extract_mod._CARD_JS:
            if not self.dom_cards:
                return []
            return [
                {"name": f"Co {i}", "text": "", "lines": ["B" if i == 0 else "C"], "profileLink": None,
                 "logoSrc": "/assets/images/default/product.webp", "dataId": f"{i:024x}"}
                for i in range(min(10, len(self.records)))
            ]
        raise AssertionError(f"unexpected evaluate: {js[:60]}")


def session() -> BrowserSession:
    cfg = {"fast_page_delay_seconds": 0, "block_http_codes": [400, 402, 403, 429, 502, 503]}
    with patch("src.session.settings", return_value=cfg):
        b = BrowserSession()
    b.settings = dict(cfg)
    b._log = lambda _m: None
    return b


def run(page, total, max_pages=10, facets="auto"):
    f = Facets(total=total) if facets == "auto" else facets
    return asyncio.run(session()._fetch_pages_fast(page, "https://ensun.io/search?q=x", max_pages, 100, f))


class TestFastPagination(unittest.TestCase):
    def test_reads_exactly_the_pages_ensun_count_needs(self):
        page = FakePage(83)
        companies, pages = run(page, 83)
        self.assertEqual((len(companies), pages), (83, 9))
        self.assertEqual(page.requested, list(range(2, 10)))  # page 10 never requested
        self.assertEqual(len({c["company_id"] for c in companies}), 83)
        self.assertEqual(companies[0]["website_url"], "https://co0.example")
        self.assertEqual(companies[0]["grade"], "B")  # grade from page-1 card
        self.assertIsNone(companies[15]["grade"])  # pages 2+ have no card

    def test_capped_at_100(self):
        page = FakePage(250)
        companies, pages = run(page, 1621)
        self.assertEqual((len(companies), pages), (100, 10))

    def test_single_page_needs_no_requests(self):
        page = FakePage(7)
        companies, pages = run(page, 7)
        self.assertEqual((len(companies), pages, page.requested), (7, 1, []))

    def test_empty_search(self):
        self.assertEqual(run(FakePage(0, dom_cards=False), 0), ([], 0))

    def test_block_mid_way_raises_for_vpn_recovery(self):
        page = FakePage(83, statuses={4: 403})
        with self.assertRaises(BlockedError) as ctx:
            run(page, 83)
        self.assertIn("page_http_403", ctx.exception.diagnostics.block_signals)
        self.assertEqual(page.requested, [2, 3, 4])

    def test_missing_page_data_falls_back_to_clicking(self):
        self.assertIsNone(run(FakePage(83, page1_from_props=False), 83))

    def test_without_counts_stops_at_short_page(self):
        page = FakePage(23)
        companies, pages = run(page, 23, facets=None)
        self.assertEqual((len(companies), pages), (23, 3))
        self.assertEqual(page.requested, [2, 3])


if __name__ == "__main__":
    unittest.main()
