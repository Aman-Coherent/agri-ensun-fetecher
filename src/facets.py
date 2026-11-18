"""Parse ensun's filter-panel counts (GraphQL ``getExpectedSearchResultCount``).

Every ensun search page fires this query; it powers the "Industries" and
"Company type" boxes with their counts, e.g. ``Manufacturer (710)``.
Verified live (2026-11-11) for q=pesticide, India:

  no filter            -> total 1621; MANUFACTURER 1320, SERVICE_PROVIDER 473, ...
  categories=MANUFACTURER -> total 1320; industries narrowed to manufacturers
  + industries=Consumer Goods and Services -> total 89

Request variables look like::

  {"queryInput": {"query": "pesticide", "continents": ["ASIA"],
                  "locations": [{"country": "India", "state": null, "city": null}],
                  "categories": ["MANUFACTURER"], "industries": ["Consumer Goods and Services"]}}

Notes: ``total`` saturates at 10000; ``aggregatedByIndustry`` lists only the
top 5 industries (exactly what the ensun UI shows).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

FACET_OPERATION = "getExpectedSearchResultCount"


@dataclass(frozen=True)
class Facets:
    total: int
    categories: list[tuple[str, int]] = field(default_factory=list)
    industries: list[tuple[str, int]] = field(default_factory=list)

    def category_count(self, category: str) -> int | None:
        return next((n for c, n in self.categories if c == category), None)

    def industry_count(self, industry: str) -> int | None:
        return next((n for i, n in self.industries if i == industry), None)

    def to_json(self) -> str:
        return json.dumps(
            {
                "total": self.total,
                "categories": [[c, n] for c, n in self.categories],
                "industries": [[i, n] for i, n in self.industries],
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str | None) -> Facets | None:
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return cls(
                total=int(data.get("total") or 0),
                categories=[(str(c), int(n)) for c, n in data.get("categories", [])],
                industries=[(str(i), int(n)) for i, n in data.get("industries", [])],
            )
        except (ValueError, TypeError, json.JSONDecodeError):
            return None


def parse_facet_body(body: Any) -> Facets | None:
    """Extract Facets from a getExpectedSearchResultCount response body."""
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    node = data.get(FACET_OPERATION)
    if not isinstance(node, dict) or node.get("total") is None:
        return None
    categories: list[tuple[str, int]] = []
    for item in node.get("aggregatedByCategory") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("category")
        count = item.get("resultCount")
        if name and isinstance(count, int):
            categories.append((str(name), count))
    industries: list[tuple[str, int]] = []
    for item in node.get("aggregatedByIndustry") or []:
        if not isinstance(item, dict):
            continue
        ind = item.get("industry") or {}
        name = ind.get("name") if isinstance(ind, dict) else None
        count = item.get("resultCount")
        if name and isinstance(count, int):
            industries.append((str(name), count))
    try:
        total = int(node["total"])
    except (TypeError, ValueError):
        return None
    return Facets(total=total, categories=categories, industries=industries)


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split()).lower()


def facet_request_matches(
    variables: Any,
    search_term: str,
    company_type: str = "",
    industry: str = "",
) -> bool:
    """True when a captured facet request belongs to this exact search.

    Guards against a late response from the *previous* page load being
    attributed to the current search.
    """
    if not isinstance(variables, dict):
        return False
    qi = variables.get("queryInput")
    if not isinstance(qi, dict):
        return False
    if _norm(qi.get("query")) != _norm(search_term):
        return False
    cats = [str(c) for c in (qi.get("categories") or [])]
    if company_type:
        if cats != [company_type]:
            return False
    elif cats:
        return False
    inds = [_norm(i) for i in (qi.get("industries") or [])]
    if industry:
        if inds != [_norm(industry)]:
            return False
    elif inds:
        return False
    return True
