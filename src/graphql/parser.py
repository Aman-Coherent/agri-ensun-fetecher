from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlencode, urlparse

from src.config_loader import settings
from src.locations import save_captured_graphql


def slugify_company_name(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def build_profile_url(
    company_name: str,
    company_id: str,
    managed: bool = False,
) -> str:
    base = settings()["ensun_base_url"].rstrip("/")
    slug = slugify_company_name(company_name)
    suffix = "-managed" if managed else ""
    return f"{base}/company/{slug}-{company_id}{suffix}"


def profile_url_candidates(company_name: str, company_id: str) -> list[str]:
    base_slug = slugify_company_name(company_name)
    simplified = slugify_company_name(
        re.sub(r"\b(GmbH|LLC|Inc|Ltd|AG|SE|Co\.)\b", "", company_name, flags=re.I)
    )
    urls: list[str] = []
    seen: set[str] = set()
    base = settings()["ensun_base_url"].rstrip("/")
    for slug in (base_slug, simplified):
        if not slug:
            continue
        for managed in (False, True):
            suffix = "-managed" if managed else ""
            url = f"{base}/company/{slug}-{company_id}{suffix}"
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def build_search_url(
    search_term: str,
    continent_code: str,
    location_triple: str,
    result_type: str | None = None,
    company_type: str | None = None,
    industry: str | None = None,
) -> str:
    params: dict[str, str] = {
        "q": search_term,
        "continents": continent_code,
        "locations": location_triple,
    }
    if result_type:
        params["resultTypes"] = result_type
    if company_type:
        params["categories"] = company_type
    if industry:
        params["industries"] = industry
    base = settings()["ensun_base_url"].rstrip("/")
    return f"{base}/search?{urlencode(params)}"


def extract_company_id(profile_url: str | None, logo_url: str | None = None) -> str | None:
    for url in (profile_url, logo_url):
        if not url:
            continue
        match = re.search(r"([a-f0-9]{24})", url)
        if match:
            return match.group(1)
    return None


def _pick(d: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in d and d[key] not in (None, ""):
            return d[key]
    return None


def normalize_company(raw: dict[str, Any]) -> dict[str, Any]:
    name = _pick(raw, "name", "companyName", "title", "displayName")
    profile_url = _pick(raw, "profileUrl", "profile_url", "url", "slug")
    if profile_url and isinstance(profile_url, str) and profile_url.startswith("/"):
        profile_url = settings()["ensun_base_url"].rstrip("/") + profile_url
    logo_url = _pick(raw, "logoUrl", "logo_url", "logo", "imageUrl")
    if logo_url and isinstance(logo_url, str) and logo_url.startswith("/"):
        logo_url = settings()["ensun_base_url"].rstrip("/") + logo_url

    location = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    city = _pick(raw, "city") or _pick(location, "city", "cityName")
    country = _pick(raw, "country") or _pick(location, "country", "countryName")

    reference = raw.get("reference") if isinstance(raw.get("reference"), dict) else {}
    if not reference and isinstance(raw.get("productReference"), dict):
        reference = raw["productReference"]

    company_id = _pick(raw, "id", "companyId", "_id")
    if company_id and not isinstance(company_id, str):
        company_id = str(company_id)
    if not company_id or len(str(company_id)) != 24:
        company_id = extract_company_id(
            profile_url if isinstance(profile_url, str) else None,
            logo_url if isinstance(logo_url, str) else None,
        )

    return {
        "company_id": company_id,
        "company_name": name,
        "city": city,
        "country": country,
        "grade": _pick(raw, "grade", "relevanceGrade", "score"),
        "employee_range": _pick(raw, "employeeRange", "employees", "employeeCountLabel"),
        "founding_year": _pick(raw, "foundingYear", "founded", "yearFounded"),
        "key_takeaway": _pick(raw, "keyTakeaway", "key_takeaway", "summary", "description"),
        "reference_type": _pick(reference, "type", "referenceType"),
        "reference_title": _pick(reference, "title", "name"),
        "reference_snippet": _pick(reference, "snippet", "description", "text"),
        "reference_url": _pick(reference, "url", "link", "externalUrl"),
        "logo_url": logo_url,
        "profile_url": profile_url,
        "raw": raw,
    }


def _looks_like_company(node: dict[str, Any]) -> bool:
    if not isinstance(node, dict):
        return False
    has_name = any(k in node for k in ("name", "companyName", "displayName", "title"))
    has_id_or_url = any(
        k in node
        for k in (
            "profileUrl",
            "profile_url",
            "logoUrl",
            "logo_url",
            "companyId",
            "id",
        )
    )
    return has_name and has_id_or_url


def extract_companies_from_json(data: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if _looks_like_company(node):
                company = normalize_company(node)
                cid = company.get("company_id") or company.get("company_name")
                key = str(cid)
                if key not in seen_ids:
                    seen_ids.add(key)
                    found.append(company)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


def extract_companies_from_responses(responses: list[Any]) -> list[dict[str, Any]]:
    all_companies: list[dict[str, Any]] = []
    seen: set[str] = set()
    for response in responses:
        for company in extract_companies_from_json(response):
            key = company.get("company_id") or company.get("company_name") or ""
            if key in seen:
                continue
            seen.add(key)
            all_companies.append(company)
    return all_companies
