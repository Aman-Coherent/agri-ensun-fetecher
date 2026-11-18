"""Extract companies from ensun search page DOM and GraphQL responses."""

from __future__ import annotations

import re
from typing import Any

from playwright.async_api import Page

from src.graphql.parser import extract_companies_from_responses, extract_company_id

# ensun shows 10 company cards per search results page
SEARCH_RESULTS_PAGE_SIZE = 10


def merge_company_lists(
    *sources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Dedupe companies by id, name, or profile URL."""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for companies in sources:
        for company in companies:
            key = (
                company.get("company_id")
                or company.get("company_name")
                or company.get("profile_url")
            )
            if not key or str(key) in seen:
                continue
            seen.add(str(key))
            merged.append(company)
    return merged


def company_dedupe_key(company: dict[str, Any]) -> str | None:
    key = (
        company.get("company_id")
        or company.get("company_name")
        or company.get("profile_url")
    )
    return str(key) if key else None


# Card = the largest element around a logo that contains no other result logo.
# Layout-independent: works for the July-2026 cards ("Key takeaway"/"Reference"
# labels) and the Nov-2026 cards (unlabelled description ending in "Source >"),
# where the old "walk up until 'Key takeaway'" search ran off the page and
# returned an empty block (city/grade/takeaway all lost).
#
# Company id: read from the logo URL when the company has its own logo. Cards
# with ensun's default picture (/assets/images/default/product.webp — ~20% of
# cards, verified 2026-11-11) carry no id there, so the id is also looked up
#   (1) in the React data behind the card (the company record ensun rendered
#       it from — works on every results page), and
#   (2) in the page's embedded Next.js data (self.__next_f — page 1),
# both matched by exact company name.
_CARD_JS = """
() => {
  const results = [];
  const seen = new Set();
  const logoSel = 'img[alt*="Logo"]';
  const isId = v => typeof v === 'string' && /^[a-f0-9]{24}$/.test(v);

  function idFromProps(props, name) {
    const visited = new Set();
    const stack = [[props, 0]];
    while (stack.length) {
      const [obj, depth] = stack.pop();
      if (!obj || typeof obj !== 'object' || visited.has(obj) || depth > 4) continue;
      visited.add(obj);
      if (isId(obj._id) && obj.name === name) return obj._id;
      for (const k of Object.keys(obj)) {
        if (k === 'children' || k === '_owner' || k === 'ref') continue;
        const v = obj[k];
        if (v && typeof v === 'object') stack.push([v, depth + 1]);
      }
    }
    return null;
  }

  function reactId(img, name) {
    try {
      const key = Object.keys(img).find(k => k.startsWith('__reactFiber$'));
      let fiber = key ? img[key] : null;
      for (let i = 0; fiber && i < 40; i++) {
        const found = idFromProps(fiber.memoizedProps, name);
        if (found) return found;
        fiber = fiber.return;
      }
    } catch (e) {}
    return null;
  }

  const payloadIds = {};
  try {
    const raw = (self.__next_f || []).map(x => (x && x[1]) || '').join('').split('\\\\"').join('"');
    const re = /"company":\\{"__typename":"\\w+","_id":"([a-f0-9]{24})","name":"((?:[^"\\\\]|\\\\.)*)"/g;
    let m;
    while ((m = re.exec(raw)) !== null) {
      let nm = m[2];
      try { nm = JSON.parse('"' + m[2] + '"'); } catch (e) {}
      if (!(nm in payloadIds)) payloadIds[nm] = m[1];
    }
  } catch (e) {}

  const imgs = Array.from(document.querySelectorAll(logoSel));
  for (const img of imgs) {
    const alt = img.getAttribute('alt') || '';
    const m = alt.match(/^(.+?)'s Logo$/);
    if (!m) continue;
    const name = m[1].trim();
    if (seen.has(name)) continue;
    seen.add(name);
    let block = null;
    let el = img.parentElement;
    while (el && el.querySelectorAll(logoSel).length === 1) {
      block = el;
      el = el.parentElement;
    }
    block = block || img.parentElement;
    const text = (block?.innerText || '').trim();
    const lines = text.split('\\n').map(s => s.trim()).filter(Boolean);
    const profileLink = block?.querySelector('a[href*="/company/"]')?.href || null;
    const logoSrc = img.getAttribute('src') || '';
    const dataId = reactId(img, name) || payloadIds[name] || null;
    results.push({ name, text, lines, profileLink, logoSrc, dataId });
  }
  return results;
}
"""

# Card UI text that is never company data.
_CARD_UI_LINES = {
    "website",
    "criteria match",
    "save",
    "phone",
    "mail",
    "source >",
    "source>",
    "lookalike companies >",
    "similar companies >",
}
_SOURCE_SUFFIX = re.compile(r"\s*Source\s*>\s*$", re.I)
_FIND_EMPLOYEES = re.compile(r"^Find [\d,.]+ employees", re.I)


_ID_RE = re.compile(r"^[a-f0-9]{24}$")


def company_id_for_card(profile_url: str | None, logo_src: str, data_id: str | None) -> str | None:
    """Profile link / own-logo URL first (exact); else the id ensun rendered the card from."""
    found = extract_company_id(profile_url, logo_src)
    if found:
        return found
    if data_id and _ID_RE.match(data_id):
        return data_id
    return None


def parse_card_lines(name: str | None, text: str, lines: list[str]) -> dict[str, Any]:
    """Pull city/country/grade/employees/year/takeaway out of a card's text lines."""
    city = country = grade = employees = founding = reference_type = None
    reference_title = None
    key_takeaway = _extract_section(text, "Key takeaway", "Reference")

    for i, line in enumerate(lines):
        if i == 0 and name and line == name:
            continue
        if re.fullmatch(r"[A-D]", line):
            grade = grade or line
        elif "Employees" in line and len(line) < 40:
            employees = employees or line.replace("Employees", "").strip()
        elif re.fullmatch(r"\d{4}", line):
            founding = founding or line
        elif (
            city is None
            and "," in line
            and len(line) < 80
            and not line.startswith(("Key", "Reference", "Find", "Similar", "Product", "Service", "Core"))
        ):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2:
                city, country = parts[0], parts[-1]
        elif line in ("Product", "Service", "Core business"):
            reference_type = line
            if i + 1 < len(lines):
                reference_title = lines[i + 1]

    if key_takeaway is None:
        # Nov-2026 layout: the summary is an unlabelled line ending in "Source >".
        for line in lines[1:]:
            clean = _SOURCE_SUFFIX.sub("", line).strip()
            if not clean or clean.lower() in _CARD_UI_LINES or _FIND_EMPLOYEES.match(clean):
                continue
            if len(clean) >= 40 and "Employees" not in clean:
                key_takeaway = clean
                break

    return {
        "city": city,
        "country": country,
        "grade": grade,
        "employee_range": employees,
        "founding_year": founding,
        "key_takeaway": key_takeaway,
        "reference_type": reference_type,
        "reference_title": reference_title,
    }


async def extract_companies_from_page(page: Page) -> list[dict[str, Any]]:
    """Parse company cards from rendered MUI search result cards."""
    raw_cards: list[dict[str, Any]] = await page.evaluate(_CARD_JS)

    parsed: list[dict[str, Any]] = []
    for raw in raw_cards:
        fields = parse_card_lines(raw.get("name"), raw.get("text", ""), raw.get("lines") or [])
        logo_src = raw.get("logoSrc") or ""
        profile_url = raw.get("profileLink")
        company_id = company_id_for_card(profile_url, logo_src, raw.get("dataId"))

        parsed.append(
            {
                "company_id": company_id,
                "company_name": raw.get("name"),
                **fields,
                "reference_snippet": None,
                "reference_url": None,
                "logo_url": logo_src or None,
                "profile_url": profile_url,
                "raw": raw,
            }
        )
    return parsed


def _extract_section(text: str, start_label: str, end_label: str) -> str | None:
    start = text.find(start_label)
    if start < 0:
        return None
    chunk = text[start + len(start_label) :]
    end = chunk.find(end_label)
    if end >= 0:
        chunk = chunk[:end]
    for stop in ("Find employees", "Similar companies"):
        idx = chunk.find(stop)
        if idx >= 0:
            chunk = chunk[:idx]
    return chunk.strip() or None


async def collect_companies_from_search(
    page: Page,
    graphql_responses: list[Any],
) -> list[dict[str, Any]]:
    from_graphql = extract_companies_from_responses(graphql_responses)
    from_dom = await extract_companies_from_page(page)
    return merge_company_lists(from_graphql, from_dom)
