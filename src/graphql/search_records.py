"""Structured search results from ensun's page data (instead of card text).

Each search result page carries its 10 results as data records (verified live
2026-11-11)::

    {"company": {"__typename": "CrawledCompanyOverview", "_id": "67a237b8...",
                 "name": "FertiStream", "description": "...",
                 "employeeCountRange": "FROM_51_TO_100", "foundingYear": null,
                 "websiteUrl": {"entry": "...", "target": "https://fertistream.com/"},
                 "logo": {"url": ".../external-company-logo/67a237b8..."},
                 "location": {"city": "Dubai", "state": "Dubai",
                              "country": "United Arab Emirates", "esgData": {...}}},
     "pageHit": {"page": {"url": {...}, "metaData": {"title": ...},
                          "classification": {"label": "PRODUCT"}, "content": [...]},
                 "takeaway": "...", "criteria": [{"citation": "...", "name": "fertilizer"}]}}

Page 1 records live in React props behind the result cards (or the embedded
Next.js data); pages 2-10 come from ``/search?...&page=N`` requests with the
``RSC: 1`` header (text/x-component), which contain the same records under a
``"results":[...]`` array.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

_ID = re.compile(r"^[a-f0-9]{24}$")
_RESULTS_KEY = re.compile(r'"results"\s*:\s*(?=\[)')


def parse_result_records(text: str) -> list[dict[str, Any]]:
    """All result records ({company, pageHit}) in an RSC / embedded-data payload, in order."""
    if not text:
        return []
    if not _RESULTS_KEY.search(text):
        text = text.replace('\\"', '"')  # embedded Next.js data is a JSON-escaped string
    decoder = json.JSONDecoder()
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    pos = 0
    while True:
        m = _RESULTS_KEY.search(text, pos)
        if not m:
            break
        start = m.end()
        try:
            arr, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            pos = start + 1
            continue
        pos = end
        if not isinstance(arr, list):
            continue
        for rec in arr:
            if not isinstance(rec, dict):
                continue
            company = rec.get("company")
            cid = company.get("_id") if isinstance(company, dict) else None
            if isinstance(cid, str) and _ID.match(cid) and cid not in seen:
                seen.add(cid)
                records.append(rec)
    return records


def employee_range_label(code: Any) -> str | None:
    """'FROM_51_TO_100' -> '51-100'; 'FROM_10001' -> '10001+' (matches the card text)."""
    if not code or not isinstance(code, str):
        return None
    m = re.fullmatch(r"FROM_(\d+)_TO_(\d+)", code)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.fullmatch(r"(?:FROM|MORE_THAN|OVER)_(\d+)(?:_TO_\w+)?", code)
    if m:
        return f"{m.group(1)}+"
    return code


def _get(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _url(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("target") or value.get("entry")
    return value if isinstance(value, str) and value else None


def _slim(rec: dict[str, Any]) -> dict[str, Any]:
    """Raw record for storage, minus the crawled page body (large, not needed)."""
    out = copy.deepcopy(rec)
    page = _get(out, "pageHit", "page")
    if isinstance(page, dict):
        page.pop("content", None)
    return out


def record_to_company(rec: dict[str, Any]) -> dict[str, Any]:
    """Map one result record to the company dict stored in search_results."""
    company = rec.get("company") or {}
    hit = rec.get("pageHit") or {}
    location = company.get("location") or {}
    criteria = hit.get("criteria") or []
    first_citation = criteria[0].get("citation") if criteria and isinstance(criteria[0], dict) else None
    founding = company.get("foundingYear")
    label = _get(hit, "page", "classification", "label")
    return {
        "company_id": company.get("_id"),
        "company_name": company.get("name"),
        "city": location.get("city"),
        "state": location.get("state"),
        "country": location.get("country"),
        "grade": None,  # card letter; filled from page-1 cards when available
        "employee_range": employee_range_label(company.get("employeeCountRange")),
        "founding_year": str(founding) if founding else None,
        "key_takeaway": hit.get("takeaway") or company.get("description"),
        "description": company.get("description"),
        "website_url": _url(company.get("websiteUrl")),
        "reference_type": label,
        "reference_title": _get(hit, "page", "metaData", "title"),
        "reference_snippet": first_citation,
        "reference_url": _url(_get(hit, "page", "url")),
        "logo_url": _get(company, "logo", "url"),
        "profile_url": None,
        "raw": _slim(rec),
    }
