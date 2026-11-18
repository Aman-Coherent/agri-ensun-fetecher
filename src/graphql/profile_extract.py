"""Merge GraphQL and DOM profile sources into a normalized record."""

from __future__ import annotations

from typing import Any

from src.graphql.parser import _pick, normalize_company
from src.graphql.profile_normalize import (
    coalesce_location,
    normalize_revenue,
    parse_founding_year,
    parse_employee_range,
    validate_profile_record,
)

PROFILE_REQUIRED = ["company_id", "company_name", "profile_url"]


def _walk_for_company_node(data: Any) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None

    def walk(node: Any) -> None:
        nonlocal best
        if isinstance(node, dict):
            name = _pick(node, "name", "companyName", "displayName")
            cid = _pick(node, "_id", "id", "companyId")
            if name and cid and isinstance(cid, str) and len(cid) == 24:
                if any(
                    k in node
                    for k in (
                        "employeeCountRange",
                        "foundingYear",
                        "websiteUrl",
                        "description",
                        "location",
                    )
                ):
                    best = node
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return best


def extract_from_graphql_responses(responses: list[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for response in responses:
        node = _walk_for_company_node(response)
        if not node:
            continue
        norm = normalize_company(node)
        loc = node.get("location") if isinstance(node.get("location"), dict) else {}
        website = node.get("websiteUrl")
        if isinstance(website, dict):
            website = website.get("target")
        out.update(
            {
                "company_id": norm.get("company_id"),
                "company_name": norm.get("company_name"),
                "description": _pick(node, "description", "summary", "about"),
                "website_url": website,
                "logo_url": norm.get("logo_url"),
                "employee_range": _pick(node, "employeeCountRange", "employeeRange")
                or norm.get("employee_range"),
                "founding_year": parse_founding_year(
                    str(_pick(node, "foundingYear", "founded") or "")
                ),
                "overview_city": _pick(loc, "city"),
                "overview_state": _pick(loc, "state"),
                "overview_country": _pick(loc, "country"),
                "working_industry": _pick(node, "industry", "workingIndustry"),
                "company_type": _pick(node, "companyType", "type"),
            }
        )
    return out


def merge_profile_data(
    company_id: str,
    profile_url: str,
    dom: dict[str, Any],
    graphql: dict[str, Any],
) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "company_id": company_id,
        "profile_url": profile_url,
    }

    scalar_keys = [
        "company_name",
        "description",
        "website_url",
        "logo_url",
        "is_managed",
        "is_claimable",
        "phone",
        "email",
        "overview_city",
        "overview_state",
        "overview_country",
        "founding_year",
        "employee_range",
        "company_size_label",
        "employee_total_visible",
        "working_industry",
        "estimated_revenue",
        "company_type",
        "ownership_structure",
        "location_count",
        "location_count_label",
        "hq_city",
        "hq_state",
        "hq_country",
        "countries_operating_count",
        "linkedin_url",
        "facebook_url",
        "twitter_url",
        "google_maps_url",
    ]

    dom_preferred = {
        "website_url",
        "linkedin_url",
        "facebook_url",
        "twitter_url",
        "google_maps_url",
        "hq_city",
        "hq_state",
        "hq_country",
        "countries_operating_count",
    }

    for key in scalar_keys:
        gval = graphql.get(key)
        dval = dom.get(key)
        if key in dom_preferred and dval not in (None, ""):
            merged[key] = dval
        elif key == "estimated_revenue":
            merged[key] = normalize_revenue(gval or dval)
        elif key in ("founding_year",):
            merged[key] = gval if gval is not None else dval
        elif key in ("employee_range",):
            merged[key] = parse_employee_range(str(gval or dval or "")) or dval or gval
        else:
            merged[key] = gval if gval not in (None, "") else dval

    loc = coalesce_location(
        {
            "city": merged.get("hq_city"),
            "state": merged.get("hq_state"),
            "country": merged.get("hq_country"),
        },
        {
            "city": merged.get("overview_city"),
            "state": merged.get("overview_state"),
            "country": merged.get("overview_country"),
        },
    )
    merged["city"] = loc["city"]
    merged["state"] = loc["state"]
    merged["country"] = loc["country"]

    merged["specialised_areas"] = dom.get("specialised_areas") or []
    merged["locations"] = dom.get("locations") or []
    merged["certificates"] = dom.get("certificates") or []
    merged["employees"] = dom.get("employees") or []
    merged["contacts"] = _build_contacts(merged, dom)
    merged["data_source"] = (
        "mixed"
        if graphql and dom
        else ("graphql" if graphql else "dom")
    )
    merged["fields_missing"] = validate_profile_record(merged, PROFILE_REQUIRED)
    merged["dom_raw"] = dom.get("dom_raw")
    merged["graphql_raw"] = graphql

    if merged.get("employee_total_visible") and not merged.get("employees"):
        merged["scrape_status"] = "partial"
    else:
        merged["scrape_status"] = "completed"

    return merged


def _build_contacts(merged: dict[str, Any], dom: dict[str, Any]) -> list[dict[str, str]]:
    contacts: list[dict[str, str]] = []
    if merged.get("phone"):
        contacts.append({"contact_type": "phone", "value": merged["phone"]})
    if merged.get("email"):
        contacts.append({"contact_type": "email", "value": merged["email"]})
    return contacts
