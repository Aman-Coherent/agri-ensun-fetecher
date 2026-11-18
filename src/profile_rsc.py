"""Extract company profile fields from Ensun Next.js RSC payload."""

from __future__ import annotations

import re
from typing import Any

EMPLOYEE_RANGE_LABELS = {
    "FROM_1_TO_10": "1-10 Employees",
    "FROM_11_TO_50": "11-50 Employees",
    "FROM_51_TO_100": "51-100 Employees",
    "FROM_101_TO_250": "101-250 Employees",
    "FROM_251_TO_500": "251-500 Employees",
    "FROM_501_TO_1000": "501-1000 Employees",
    "FROM_1001_TO_5000": "1001-5000 Employees",
    "FROM_5001_TO_10000": "5001-10000 Employees",
    "FROM_10000_UPWARDS": "10001+ Employees",
}

CATEGORY_LABELS = {
    "MANUFACTURER": "Manufacturer",
    "SERVICE_PROVIDER": "Service provider",
    "DISTRIBUTOR": "Distributor",
    "WHOLESALER": "Wholesaler",
}

COMPANY_OVERVIEW_RE = re.compile(
    r'CrawledCompanyOverview\\",\\"_id\\":\\"([a-f0-9]{24})\\",\\"name\\":\\"((?:[^"\\]|\\.)*?)\\"',
)


def unescape_rsc(value: str) -> str:
    return (
        value.replace("\\u0026", "&")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )


def is_valid_company_name(name: str) -> bool:
    if not name or len(name.strip()) < 2:
        return False
    lower = name.lower().strip()
    if re.search(r"\.(jpg|jpeg|png|svg|webp|gif|ico)$", lower):
        return False
    if "header-find" in lower or "find-us-company" in lower:
        return False
    if lower.startswith("self.__next_f"):
        return False
    if re.fullmatch(r"[a-f0-9_-]{20,}", lower):
        return False
    return True


def rsc_field(window: str, key: str) -> str:
    match = re.search(rf'\\"{re.escape(key)}\\":\\"((?:[^"\\]|\\.)*?)\\"', window)
    if match:
        return unescape_rsc(match.group(1))
    match = re.search(rf'\\"{re.escape(key)}\\":(\d+)', window)
    return match.group(1) if match else ""


def rsc_location_field(window: str, key: str) -> str:
    loc_idx = window.find('\\"location\\":{')
    if loc_idx < 0:
        return ""
    sub = window[loc_idx : loc_idx + 1200]
    return rsc_field(sub, key)


_LOGO_FILE_RE = re.compile(
    r"https?://[^\"'\\]+\.(?:ensun\.io|platform-production\.ensun\.io)"
    r"/file/(?:external-company-logo|managed-company-logo)/[a-f0-9]{24}",
    re.I,
)


def rsc_logo_url(window: str) -> str:
    for pattern in (
        r'\\"logo\\":\{[^}]*\\"url\\":\\"([^\\"]+)\\"',
        r'\\"url\\":\\"(https?://[^\\"]*(?:external-company-logo|managed-company-logo)/[a-f0-9]{24}[^\\"]*)\\"',
    ):
        match = re.search(pattern, window)
        if match:
            return unescape_rsc(match.group(1))
    return ""


def extract_logo_from_html(html: str, company_id: str = "") -> str:
    """Fallback when nested RSC logo JSON is outside the primary window."""
    if company_id:
        for pattern in (
            rf'\\"url\\":\\"(https?://[^\\"]+/{re.escape(company_id)}[^\\"]*)\\"',
            rf'(https?://[^"\'\\s]+/(?:external-company-logo|managed-company-logo)/{re.escape(company_id)}[^"\'\\s]*)',
        ):
            match = re.search(pattern, html)
            if match:
                return unescape_rsc(match.group(1))
    for pattern in (
        r'property="og:image"\s+content="([^"]+)"',
        r'content="([^"]+)"\s+property="og:image"',
    ):
        match = re.search(pattern, html, re.I)
        if match:
            url = unescape_rsc(match.group(1))
            if "company-logo" in url or "ensun.io/file" in url:
                return url
    match = _LOGO_FILE_RE.search(html)
    return match.group(0) if match else ""


def rsc_categories(window: str) -> str:
    match = re.search(r'\\"categories\\":\[([^\]]*)\]', window)
    if not match:
        return ""
    raw = match.group(1)
    labels = []
    for token in re.findall(r'\\"([A-Z_]+)\\"', raw):
        labels.append(CATEGORY_LABELS.get(token, token.replace("_", " ").title()))
    return ", ".join(labels)


def rsc_industries(window: str) -> str:
    names = re.findall(r'\\"industries\\":\[\{\\"name\\":\\"([^\\"]+)\\"', window)
    if not names:
        idx = window.find('\\"industries\\"')
        if idx >= 0:
            names = re.findall(
                r'\\"name\\":\\"([^\\"]+)\\"', window[idx : idx + 500]
            )
    return ", ".join(dict.fromkeys(names))


def rsc_website(window: str) -> str:
    match = re.search(r'\\"websiteUrl\\":\{\\"target\\":\\"([^\\"]+)\\"', window)
    return unescape_rsc(match.group(1)) if match else ""


def rsc_company_name(window: str) -> str:
    match = re.search(r'\\"name\\":\\"([^\\"]+)\\",\\"websiteUrl\\"', window)
    if match:
        name = unescape_rsc(match.group(1))
        if is_valid_company_name(name):
            return name
    match = COMPANY_OVERVIEW_RE.search(window)
    if match:
        return unescape_rsc(match.group(2))
    return ""


def parse_company_detail_rsc(html: str, slug: str) -> dict[str, Any] | None:
    start = html.find('\\"company\\":{')
    if start < 0:
        start = html.find("CrawledCompanyOverview")
    if start < 0:
        return None

    window = html[start : start + 30000]
    mongo_match = re.search(r'\\"_id\\":\\"([a-f0-9]{24})\\"', window)
    if not mongo_match:
        return None

    name = rsc_company_name(window)
    if not is_valid_company_name(name):
        name = slug.rsplit("-", 1)[0].replace("-", " ").title()

    employee_range = rsc_field(window, "employeeCountRange")
    rev_text = ""
    rev_match = re.search(
        r'\\"revenueRangeEstimationMio\\":\[(\d+|null),(\d+|null)\]', window
    )
    if rev_match:
        lo, hi = rev_match.group(1), rev_match.group(2)
        if lo != "null" and hi != "null":
            rev_text = f"{lo} - {hi} Million USD"
        elif hi != "null":
            rev_text = f"< {hi} Million USD"

    legal = rsc_field(window, "legalform").replace("_", " ").title()
    keywords = re.findall(r'\\"keywords\\":\[(.*?)\]', window, re.DOTALL)
    kw_list: list[str] = []
    if keywords:
        kw_list = re.findall(r'\\"([^\\"]+)\\"', keywords[0])[:20]

    return {
        "company_id": mongo_match.group(1),
        "company_name": name,
        "description": rsc_field(window, "description"),
        "website_url": rsc_website(window),
        "logo_url": rsc_logo_url(window) or extract_logo_from_html(html, mongo_match.group(1)),
        "hq_city": rsc_location_field(window, "city"),
        "hq_state": rsc_location_field(window, "state"),
        "hq_country": rsc_location_field(window, "country"),
        "founding_year": rsc_field(window, "foundingYear"),
        "employee_range": EMPLOYEE_RANGE_LABELS.get(employee_range, employee_range),
        "working_industry": rsc_industries(window),
        "estimated_revenue": rev_text or rsc_field(window, "revenueRangeEstimationMio"),
        "company_type": rsc_categories(window),
        "ownership_structure": legal,
        "specialised_areas": kw_list,
    }
