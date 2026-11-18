"""Company ids for search cards — including cards with ensun's default logo.

Runs the real card-extraction JavaScript in headless Chromium against small
offline pages (no network). Mirrors the live structure seen 2026-11-11.
"""

from __future__ import annotations

import asyncio
import unittest

from src.graphql.extract import company_id_for_card, extract_companies_from_page

OWN_LOGO_ID = "64a54ab10b60192b1bcd9ea1"
PAYLOAD_ID = "649d2a668aec839b8f0ea5e1"
REACT_ID = "67ae73d536cfd2c5ba30ee8e"
INDUSTRY_ID = "665444828e61d1b893ee2005"


def card(name: str, src: str, extra: str = "") -> str:
    return f"""
    <div class="MuiPaper-root"><div><img alt="{name}'s Logo" src="{src}" {extra}></div>
      <p>{name}</p><p>Pune, India</p><p>B</p><p>11-50 Employees</p><p>2001</p>
    </div>"""


PAGE = f"""<!doctype html><html><body>
<script>
  self.__next_f = self.__next_f || [];
  // industries share the {{_id, name}} shape and must NOT be taken as companies
  self.__next_f.push([1, '{{\\"getAllIndustries\\":[{{\\"_id\\":\\"{INDUSTRY_ID}\\",\\"name\\":\\"Chemicals\\"}}]}}']);
  self.__next_f.push([1, '[{{\\"company\\":{{\\"__typename\\":\\"CrawledCompanyOverview\\",\\"_id\\":\\"{PAYLOAD_ID}\\",\\"name\\":\\"Crystal Crop Protection\\",\\"description\\":\\"x\\"}}}}]']);
</script>
<div class="MuiStack-root">
  {card("Willowood", "https://api.platform-production.ensun.io/file/external-company-logo/" + OWN_LOGO_ID)}
  {card("Crystal Crop Protection", "/assets/images/default/product.webp")}
  {card("ARBUDA AGROCHEMICALS", "/assets/images/default/product.webp", 'id="arbuda"')}
  {card("Chemicals", "/assets/images/default/product.webp")}
  {card("Nameless Co", "/assets/images/default/product.webp")}
</div>
<script>
  // what React attaches to rendered elements: a fiber chain whose props hold the record
  document.getElementById('arbuda')['__reactFiber$test'] = {{
    memoizedProps: {{alt: 'x'}},
    return: {{memoizedProps: {{result: {{company: {{_id: '{REACT_ID}', name: 'ARBUDA AGROCHEMICALS'}}}}}}, return: null}},
  }};
</script>
</body></html>"""


class TestCompanyIdForCard(unittest.TestCase):
    def test_logo_id_wins(self):
        self.assertEqual(
            company_id_for_card(None, f"https://x/external-company-logo/{OWN_LOGO_ID}", REACT_ID), OWN_LOGO_ID
        )

    def test_data_id_used_for_default_logo(self):
        self.assertEqual(company_id_for_card(None, "/assets/images/default/product.webp", REACT_ID), REACT_ID)

    def test_invalid_data_id_ignored(self):
        self.assertIsNone(company_id_for_card(None, "/assets/images/default/product.webp", "not-an-id"))


class TestCardIdsInBrowser(unittest.TestCase):
    def test_all_sources(self):
        async def run():
            from playwright.async_api import async_playwright

            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.set_content(PAGE)
                    return await extract_companies_from_page(page)
                finally:
                    await browser.close()

        try:
            companies = asyncio.run(run())
        except Exception as exc:  # e.g. Chromium not installed
            self.skipTest(f"browser unavailable: {exc}")
        ids = {c["company_name"]: c["company_id"] for c in companies}
        self.assertEqual(ids["Willowood"], OWN_LOGO_ID)
        self.assertEqual(ids["Crystal Crop Protection"], PAYLOAD_ID)  # embedded page data
        self.assertEqual(ids["ARBUDA AGROCHEMICALS"], REACT_ID)  # React record behind card
        self.assertIsNone(ids["Chemicals"])  # industry record never matched as a company
        self.assertIsNone(ids["Nameless Co"])
        crystal = next(c for c in companies if c["company_name"] == "Crystal Crop Protection")
        self.assertEqual((crystal["city"], crystal["grade"]), ("Pune", "B"))


if __name__ == "__main__":
    unittest.main()
