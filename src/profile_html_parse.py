"""Parse Ensun company profile HTML (SSR) into dom dict for merge_profile_data."""

from __future__ import annotations

import re
from typing import Any

from selectolax.parser import HTMLParser

from src.graphql.profile_normalize import (
    is_valid_profile_description,
    parse_countries_operating,
    parse_employee_range,
    parse_employee_total,
    parse_founding_year,
    parse_labeled_line,
    parse_location_count,
    split_specialised_areas,
)
from src.graphql.profile_locations import normalize_google_maps_url
from src.profile_rsc import extract_logo_from_html, parse_company_detail_rsc

CHECKPOINT_MARKERS = (
    "vercel security checkpoint",
    "verifying your browser",
    "failed to verify your browser",
    "security-checkpoint",
    "checking your browser",
    "cf-browser-verification",
)

_ENSUN_LINK_MARKERS = ("ensun.io", "ensun-gmbh")
_SOCIAL_DOMAINS = {
    "linkedin.com": "linkedin_url",
    "facebook.com": "facebook_url",
    "twitter.com": "twitter_url",
    "x.com": "twitter_url",
}


class CheckpointPageError(Exception):
    """Vercel/bot checkpoint HTML returned instead of profile."""


def is_checkpoint_html(html: str, title: str = "") -> bool:
    """True only for actual Vercel/bot challenge pages, not normal SSR profiles."""
    if not html or len(html) < 500:
        return True
    lower = html.lower()
    title_lower = title.lower()
    if any(m in lower for m in CHECKPOINT_MARKERS):
        return True
    if any(m in title_lower for m in CHECKPOINT_MARKERS):
        return True
    if "code 21" in lower or "code 11" in lower:
        return True
    if "CrawledCompanyOverview" in html or '\\"company\\":{' in html:
        return False
    if "<h1" in lower and "/company/" in lower:
        return False
    return False


_SPEC_END_MARKERS = (
    "Find companies 10x",
    "ESG score estimation",
    "Certificates of",
    "Company lists",
    "Headquarter of",
)


def _normalize_external_url(url: str | None) -> str | None:
    if not url:
        return None
    if ":443/" in url:
        return url.replace(":443/", "/")
    return url


def _section_slice(text: str, start_label: str, end_labels: tuple[str, ...]) -> str:
    idx = text.find(start_label)
    if idx < 0:
        return ""
    chunk = text[idx + len(start_label) :]
    end = len(chunk)
    for label in end_labels:
        pos = chunk.find(label)
        if pos >= 0:
            end = min(end, pos)
    return chunk[:end].strip()


def _body_lines(tree: HTMLParser) -> list[str]:
    text = tree.body.text(separator="\n") if tree.body else ""
    return [
        ln.strip()
        for ln in text.split("\n")
        if ln.strip() and not ln.strip().startswith("self.__next_f")
    ]


def _find_pattern(lines: list[str], pattern: str, flags: int = 0) -> str:
    for line in lines:
        match = re.search(pattern, line, flags)
        if match:
            return match.group(1).strip()
    return ""


_ENSUN_SOCIAL_RE = re.compile(
    r"linkedin\.com/company/ensun|facebook\.com/.*ensun|twitter\.com/.*ensun", re.I
)


def _is_ensun_social(href: str) -> bool:
    return bool(_ENSUN_SOCIAL_RE.search(href))


def _extract_locations_tab_data(tree: HTMLParser, body_text: str) -> dict[str, str | None]:
    """HQ card links: website, social, Google Maps (full-page scan)."""
    result: dict[str, str | None] = {
        "website_url": None,
        "linkedin_url": None,
        "facebook_url": None,
        "twitter_url": None,
        "google_maps_url": None,
    }

    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href.startswith("http"):
            continue
        if any(m in href for m in _ENSUN_LINK_MARKERS):
            continue
        text = (node.text(strip=True) or "").lower()
        aria = (node.attributes.get("aria-label") or "").lower()
        if "linkedin.com" in href and not _is_ensun_social(href) and not result["linkedin_url"]:
            result["linkedin_url"] = href
        elif "facebook.com" in href and not _is_ensun_social(href) and not result["facebook_url"]:
            result["facebook_url"] = href
        elif ("twitter.com" in href or "x.com/" in href) and not _is_ensun_social(href) and not result["twitter_url"]:
            result["twitter_url"] = href
        elif "google.com/maps" in href or "maps.google.com" in href:
            if not result["google_maps_url"]:
                result["google_maps_url"] = href
        elif text == "website" or aria == "website" or (not result["website_url"] and "website" in text):
            result["website_url"] = href
        elif not result["website_url"] and href.startswith("http") and "ensun" not in href:
            # First external link near locations section often is website.
            pass

    for iframe in tree.css("iframe"):
        src = (iframe.attributes.get("src") or "").strip()
        if "google.com/maps" in src and not result["google_maps_url"]:
            result["google_maps_url"] = normalize_google_maps_url(src)

    # Fallback: website from RSC-adjacent links in body if still missing
    if not result["website_url"] and "Headquarter of" in body_text:
        for node in tree.css('a[href^="http"]'):
            href = (node.attributes.get("href") or "").strip()
            if href and not any(m in href for m in _ENSUN_LINK_MARKERS):
                if not any(d in href for d in _SOCIAL_DOMAINS) and "google.com/maps" not in href:
                    result["website_url"] = href
                    break

    if result["google_maps_url"]:
        result["google_maps_url"] = normalize_google_maps_url(result["google_maps_url"])
    if result["website_url"]:
        result["website_url"] = _normalize_external_url(result["website_url"])
    return result


def _extract_overview_size_label(body_text: str) -> str | None:
    overview = _section_slice(
        body_text,
        "Quick overview",
        (
            "Additional information",
            "Specialised areas",
            "ESG score estimation",
            "Company lists",
            "Headquarter of",
            "Locations of",
        ),
    )
    for ln in overview.split("\n"):
        ln = ln.strip()
        match = re.search(r"(Small|Medium|Large)[\s-]*Sized\s+Company", ln, re.I)
        if match:
            return match.group(0)
    match = re.search(r"(Small|Medium|Large)[\s-]*Sized\s+Company", body_text, re.I)
    return match.group(0) if match else None


def _extract_description_after_h1(h1: Any) -> str | None:
    if not h1:
        return None
    el = h1.next
    while el:
        tag = el.tag
        if tag in ("h2", "h3"):
            break
        t = el.text(strip=True)
        if is_valid_profile_description(t) and "Claim this company" not in t:
            return t
        el = el.next
    return None


def _apply_rsc_field(dom: dict[str, Any], key: str, value: Any) -> None:
    if not value:
        return
    if key == "description" and not is_valid_profile_description(str(value)):
        return
    if not dom.get(key):
        dom[key] = value


def _extract_certificates(tree: HTMLParser, lines: list[str]) -> list[dict[str, Any]]:
    certs: list[dict[str, Any]] = []
    for table in tree.css("table"):
        header = (table.text() or "")[:80]
        if "Name" not in header or "Status" not in header:
            continue
        for tr in table.css("tr"):
            cells = [td.text(strip=True) for td in tr.css("td, th")]
            if len(cells) >= 3 and cells[0] != "Name":
                certs.append(
                    {
                        "certificate_name": cells[0],
                        "certificate_status": cells[1],
                        "certificate_description": cells[2],
                    }
                )
    if certs:
        return certs
    idx = "\n".join(lines).find("Certificates of")
    if idx < 0:
        return []
    chunk = "\n".join(lines)[idx : idx + 2000]
    chunk_lines = [ln.strip() for ln in chunk.split("\n") if ln.strip()]
    i = 0
    while i + 2 < len(chunk_lines):
        name, status, desc = chunk_lines[i], chunk_lines[i + 1], chunk_lines[i + 2]
        if name.lower().startswith("certificates"):
            i += 1
            continue
        if status in ("Active", "Inactive", "Expired", "Pending"):
            certs.append(
                {
                    "certificate_name": name,
                    "certificate_status": status,
                    "certificate_description": desc,
                }
            )
            i += 3
        else:
            i += 1
    return certs


def _extract_locations_table(tree: HTMLParser) -> list[dict[str, Any]]:
    locs: list[dict[str, Any]] = []
    for table in tree.css("table"):
        header = (table.text() or "")[:60]
        if not all(k in header for k in ("Location", "Country", "City")):
            continue
        for tr in table.css("tr"):
            cells = [td.text(strip=True) for td in tr.css("td")]
            if len(cells) >= 4:
                locs.append(
                    {
                        "location_role": cells[0],
                        "country": cells[1],
                        "state": cells[2],
                        "city": cells[3],
                    }
                )
    return locs


def _extract_employee_cards(tree: HTMLParser) -> list[dict[str, Any]]:
    employees: list[dict[str, Any]] = []
    for card in tree.css('[class*="MuiPaper"], [class*="mui-"]'):
        text = card.text() or ""
        if "@" not in text or len(text) > 500:
            continue
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if len(lines) < 2:
            continue
        email_match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", text)
        linkedin = ""
        for a in card.css('a[href*="linkedin.com"]'):
            linkedin = a.attributes.get("href") or ""
            break
        employees.append(
            {
                "full_name": lines[0],
                "job_title": lines[1] if len(lines) > 1 else None,
                "email": email_match.group(0) if email_match else None,
                "linkedin_url": linkedin or None,
            }
        )
    return employees


def parse_profile_html_to_dom(
    html: str,
    *,
    company_id: str,
    company_name: str,
    profile_url: str,
) -> dict[str, Any]:
    if is_checkpoint_html(html):
        raise CheckpointPageError("Checkpoint page returned for company profile")

    tree = HTMLParser(html)
    lines = _body_lines(tree)
    rsc = parse_company_detail_rsc(html, profile_url.rsplit("/", 1)[-1])

    h1 = tree.css_first("h1")
    dom_name = h1.text(strip=True) if h1 else company_name

    body_text = tree.body.text(separator="\n") if tree.body else ""
    loc_data = _extract_locations_tab_data(tree, body_text)

    tel = tree.css_first('a[href^="tel:"]')
    mail = tree.css_first('a[href^="mailto:"]')
    employee_total = parse_employee_total(body_text)

    hq_block = ""
    if "Headquarter of" in body_text:
        start = body_text.index("Headquarter of")
        end = body_text.find("Locations of", start)
        if end < 0:
            end = start + 900
        hq_block = body_text[start:end]

    certificates = _extract_certificates(tree, lines)
    locations = _extract_locations_table(tree)
    employees = _extract_employee_cards(tree)

    specialised_text = _section_slice(body_text, "Specialised areas", _SPEC_END_MARKERS)

    dom: dict[str, Any] = {
        "company_name": dom_name or company_name,
        "description": None,
        "website_url": loc_data.get("website_url"),
        "logo_url": None,
        "phone": tel.attributes.get("href", "").replace("tel:", "") if tel else None,
        "email": mail.attributes.get("href", "").replace("mailto:", "") if mail else None,
        "is_claimable": 1 if "Claim this company" in body_text else 0,
        "is_managed": 1 if profile_url.endswith("-managed") else 0,
        "employee_total_visible": employee_total,
        "founding_year": parse_founding_year(
            _find_pattern(lines, r"Founded in (\d{4})")
        ),
        "employee_range": parse_employee_range(
            _find_pattern(
                lines,
                r"((?:\d+\s*-\s*\d+|\d+\+)\s*Employees)",
                re.I,
            )
        ),
        "working_industry": _find_pattern(lines, r"Working industry:\s*(.+)") or None,
        "estimated_revenue": _find_pattern(lines, r"Estimated revenue:\s*(.+)") or None,
        "company_type": _find_pattern(lines, r"Type of company:\s*(.+)") or None,
        "ownership_structure": _find_pattern(lines, r"Ownership structure:\s*(.+)") or None,
        "company_size_label": _extract_overview_size_label(body_text),
        "location_count": parse_location_count(body_text),
        "countries_operating_count": parse_countries_operating(hq_block or body_text),
        "hq_city": parse_labeled_line(hq_block, "City"),
        "hq_state": parse_labeled_line(hq_block, "State"),
        "hq_country": parse_labeled_line(hq_block, "Country"),
        "linkedin_url": loc_data.get("linkedin_url"),
        "facebook_url": loc_data.get("facebook_url"),
        "twitter_url": loc_data.get("twitter_url"),
        "google_maps_url": loc_data.get("google_maps_url"),
        "specialised_areas": split_specialised_areas(
            specialised_text.replace("\n", ", ")
        ),
        "locations": locations,
        "certificates": certificates,
        "employees": employees,
        "dom_raw": {"source": "http_html", "body_len": len(html)},
    }

    if rsc:
        for key in (
            "description",
            "website_url",
            "logo_url",
            "hq_city",
            "hq_state",
            "hq_country",
            "founding_year",
            "working_industry",
            "estimated_revenue",
            "company_type",
            "ownership_structure",
        ):
            _apply_rsc_field(dom, key, rsc.get(key))
        dom_er = parse_employee_range(str(dom.get("employee_range") or ""))
        rsc_er = parse_employee_range(str(rsc.get("employee_range") or ""))
        if rsc_er and not dom_er:
            dom["employee_range"] = rsc_er
        elif dom_er:
            dom["employee_range"] = dom_er
        if rsc.get("specialised_areas") and not dom["specialised_areas"]:
            if isinstance(rsc["specialised_areas"], list):
                dom["specialised_areas"] = rsc["specialised_areas"]
            else:
                dom["specialised_areas"] = [
                    s.strip() for s in str(rsc["specialised_areas"]).split("|")
                ]

    cid = (rsc or {}).get("company_id") or company_id
    if not dom.get("logo_url"):
        dom["logo_url"] = extract_logo_from_html(html, cid) or None

    if not is_valid_profile_description(dom.get("description")):
        dom["description"] = _extract_description_after_h1(h1)

    return dom
