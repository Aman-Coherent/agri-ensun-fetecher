"""Parsing ensun's search-result data records (shape captured live 2026-11-11)."""

from __future__ import annotations

import json
import unittest

from src.graphql.search_records import (
    employee_range_label,
    parse_result_records,
    record_to_company,
)


def rec(cid: str, name: str, **company) -> dict:
    return {
        "company": {
            "__typename": "CrawledCompanyOverview",
            "_id": cid,
            "name": name,
            "description": f"{name} makes fertilizers.",
            "employeeCountRange": "FROM_51_TO_100",
            "foundingYear": None,
            "websiteUrl": {"entry": "https://fertistream.com/", "target": "https://fertistream.com/"},
            "logo": {"url": f"https://api.platform-production.ensun.io/file/external-company-logo/{cid}"},
            "location": {"city": "Dubai", "state": "Dubai", "country": "United Arab Emirates",
                         "esgData": {"environment": {"grade": "D"}}},
            **company,
        },
        "pageHit": {
            "page": {
                "__typename": "CrawledPage",
                "url": {"entry": "https://fertistream.com/products", "target": "https://fertistream.com/products"},
                "content": [{"type": "h1", "text": "big page body " * 50}],
                "metaData": {"title": "High-Quality Fertilizers"},
                "classification": {"label": "PRODUCT", "isVerified": True},
            },
            "takeaway": f"{name} manufactures fertilizers.",
            "criteria": [{"signalStrength": 3, "citation": "produces fertilizers", "name": "fertilizer"}],
        },
    }


FERTI = rec("67a237b8af9646dbcc1a7edc", "FertiStream")
OTHER = rec("67a237b8af9646dbcc1a7edd", "Vgrow Prime")


def rsc(*arrays) -> str:
    """Roughly how a text/x-component payload looks: numbered lines of JSON."""
    lines = ['0:["$","$L1",null,{"children":"x"}]']
    for i, arr in enumerate(arrays, start=1):
        lines.append(f'{i:x}:["$","$L2",null,{{"results":{json.dumps(arr)},"getCompanyPath":"$undefined"}}]')
    return "\n".join(lines)


class TestParse(unittest.TestCase):
    def test_rsc_payload(self):
        text = rsc([{"foo": 1}], [FERTI, OTHER])
        recs = parse_result_records(text)
        self.assertEqual([r["company"]["name"] for r in recs], ["FertiStream", "Vgrow Prime"])

    def test_duplicates_across_arrays_kept_once(self):
        self.assertEqual(len(parse_result_records(rsc([FERTI], [FERTI, OTHER]))), 2)

    def test_escaped_embedded_payload(self):
        escaped = rsc([FERTI]).replace('"', '\\"')  # how it sits inside self.__next_f strings
        self.assertEqual(parse_result_records(escaped)[0]["company"]["_id"], FERTI["company"]["_id"])

    def test_garbage(self):
        for text in ("", "nothing", '"results":[broken', '"results":"x"', '"results":[{"company":{"_id":"short"}}]'):
            self.assertEqual(parse_result_records(text), [])


class TestMapping(unittest.TestCase):
    def test_record_to_company(self):
        c = record_to_company(FERTI)
        self.assertEqual(c["company_id"], "67a237b8af9646dbcc1a7edc")
        self.assertEqual((c["city"], c["state"], c["country"]), ("Dubai", "Dubai", "United Arab Emirates"))
        self.assertEqual(c["website_url"], "https://fertistream.com/")
        self.assertEqual(c["employee_range"], "51-100")
        self.assertIsNone(c["founding_year"])
        self.assertEqual(c["key_takeaway"], "FertiStream manufactures fertilizers.")
        self.assertEqual(c["description"], "FertiStream makes fertilizers.")
        self.assertEqual(c["reference_type"], "PRODUCT")
        self.assertEqual(c["reference_url"], "https://fertistream.com/products")
        self.assertEqual(c["reference_snippet"], "produces fertilizers")
        self.assertTrue(c["logo_url"].endswith(c["company_id"]))
        self.assertNotIn("content", c["raw"]["pageHit"]["page"])  # big page body dropped
        self.assertIn("content", FERTI["pageHit"]["page"])  # original untouched

    def test_founding_year_and_missing_parts(self):
        c = record_to_company(rec("67a237b8af9646dbcc1a7edf", "X", foundingYear=1992, websiteUrl=None, location=None))
        self.assertEqual(c["founding_year"], "1992")
        self.assertIsNone(c["website_url"])
        self.assertIsNone(c["city"])

    def test_employee_ranges(self):
        self.assertEqual(employee_range_label("FROM_1001_TO_5000"), "1001-5000")
        self.assertEqual(employee_range_label("FROM_10001"), "10001+")
        self.assertIsNone(employee_range_label(None))
        self.assertEqual(employee_range_label("UNKNOWN"), "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
