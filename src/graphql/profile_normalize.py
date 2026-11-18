from __future__ import annotations

import re
from typing import Any

EMPLOYEE_CATEGORIES = ("C-Level", "Procurement", "Sales", "Other")
ESG_GRADES = frozenset("ABCDE")


def slugify_company_name(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def parse_founding_year(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"(?:Founded in|founded in)\s*(\d{4})", text, re.I)
    if match:
        return int(match.group(1))
    if re.fullmatch(r"\d{4}", text.strip()):
        return int(text.strip())
    return None


_GENERIC_DESC_PREFIX = "get all information about"


def is_valid_profile_description(text: str | None) -> bool:
    """Reject corrupt RSC/meta placeholders; keep real overview paragraphs."""
    if not text:
        return False
    cleaned = re.sub(r"\s+", " ", text.strip())
    if len(cleaned) < 40:
        return False
    if re.fullmatch(r"\$[0-9a-f]{1,6}", cleaned, re.I):
        return False
    if cleaned.lower().startswith(_GENERIC_DESC_PREFIX):
        return False
    alpha = sum(1 for c in cleaned if c.isalpha())
    return alpha >= 20


def parse_employee_range(text: str | None) -> str | None:
    """Parse Ensun overview employee band (e.g. 1-10), not paid-list counts."""
    if not text:
        return None
    cleaned = re.sub(r"\s*Employees\s*$", "", text.strip(), flags=re.I)
    cleaned = re.sub(r"\s+", "", cleaned)
    if not cleaned or cleaned == "0":
        return None
    # Overview ranges use a hyphen or trailing + (10001+), never a bare count.
    if "-" in cleaned or cleaned.endswith("+"):
        return cleaned
    return None


def parse_employee_total(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"See\s+(\d+)\s+employees?", text, re.I)
    return int(match.group(1)) if match else None


def parse_location_line(line: str | None) -> dict[str, str | None]:
    if not line:
        return {"city": None, "state": None, "country": None}
    parts = [p.strip() for p in line.split(",") if p.strip()]
    if len(parts) >= 3:
        return {"city": parts[0], "state": parts[1], "country": parts[2]}
    if len(parts) == 2:
        return {"city": parts[0], "state": None, "country": parts[1]}
    if len(parts) == 1:
        return {"city": parts[0], "state": None, "country": None}
    return {"city": None, "state": None, "country": None}


def parse_labeled_line(text: str | None, label: str) -> str | None:
    if not text:
        return None
    pattern = rf"{re.escape(label)}:\s*(.+)"
    match = re.search(pattern, text, re.I)
    return match.group(1).strip() if match else None


def parse_location_count(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"(\d+)\s+Headquarter", text, re.I)
    return int(match.group(1)) if match else None


def parse_countries_operating(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"operates in (\d+) countr", text, re.I)
    return int(match.group(1)) if match else None


def parse_list_card_meta(text: str | None) -> tuple[int | None, int | None]:
    if not text:
        return None, None
    companies = countries = None
    m1 = re.search(r"(\d+)\s*\+?\s*suitable companies", text, re.I)
    if m1:
        companies = int(m1.group(1))
    m2 = re.search(r"Companies in (\d+)\s*\+?\s*countries", text, re.I)
    if m2:
        countries = int(m2.group(1))
    return companies, countries


def parse_phone(text: str | None) -> str | None:
    if not text:
        return None
    text = text.strip()
    if text.startswith("tel:"):
        return text[4:].strip()
    match = re.search(r"(\+\d[\d\s\-()]{7,}\d)", text)
    return match.group(1).strip() if match else None


def parse_email(text: str | None) -> str | None:
    if not text:
        return None
    if text.startswith("mailto:"):
        return text[7:].split("?")[0].strip()
    match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", text)
    return match.group(0) if match else None


def parse_esg_grade(text: str | None) -> str | None:
    if not text:
        return None
    letter = text.strip().upper()
    if len(letter) == 1 and letter in ESG_GRADES:
        return letter
    match = re.search(r"\b([A-E])\b", text)
    return match.group(1) if match else None


def split_specialised_areas(text: str | None) -> list[str]:
    if not text:
        return []
    parts = re.split(r",\s*", text)
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        name = part.strip()
        if not name or name in ("---", "-"):
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name[:200])
    return out


def normalize_revenue(text: str | None) -> str | None:
    if not text:
        return None
    return re.sub(r"\s+", " ", text.strip()) or None


def coalesce_location(
    hq: dict[str, str | None],
    overview: dict[str, str | None],
) -> dict[str, str | None]:
    return {
        "city": hq.get("city") or overview.get("city"),
        "state": hq.get("state") or overview.get("state"),
        "country": hq.get("country") or overview.get("country"),
    }


def validate_profile_record(record: dict[str, Any], required: list[str]) -> list[str]:
    missing: list[str] = []
    for key in required:
        val = record.get(key)
        if val is None or val == "" or val == []:
            missing.append(key)
    return missing
