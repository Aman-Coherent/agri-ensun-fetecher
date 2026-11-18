"""CSV exports for one country database.

search_by_entity  one row per (entity, company); all of that entity's search
                  terms that found the company merged into one cell.
entity_full       same rows + profile details (website, phone, email, ...).
company_profiles  full profile sheet, one row per company.
companies_websites  company name + website.

Files are written as UTF-8 with BOM so Excel shows accents correctly, via a
temp file + rename so a crash never leaves a half-written CSV.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from src.config_loader import OUTPUT_DIR, country_slug, ensure_dirs
from src.graphql.profile_flatten import PROFILE_EXPORT_COLUMNS, format_profile_csv_cell

if TYPE_CHECKING:
    from src.storage.country_db import CountryDatabase

# Search terms can contain commas ("2,4-D amine salt 58% SL"), so multi-value
# cells use " | " as the separator.
MULTI_SEP = " | "

COMPANY_TYPE_LABELS = {
    "MANUFACTURER": "Manufacturer",
    "SERVICE_PROVIDER": "Service provider",
    "DISTRIBUTOR": "Distributor",
    "EDUCATIONAL_INSTITUTION": "Educational institution",
    "RETAILER": "Retailer",
}

ENTITY_SEARCH_COLUMNS = [
    "category",
    "entity_no",
    "entity_name",
    "company_id",
    "company_name",
    "website_url",
    "city",
    "state",
    "country",
    "search_terms",
    "search_term_count",
    "sub_groups",
    "company_types",
    "industries",
    "grade",
    "employee_range",
    "founding_year",
    "key_takeaway",
    "description",
    "profile_url",
    "logo_url",
    "continent",
    "searched_country",
]

# website_url is handled separately: profile website if scraped, else the search one.
PROFILE_COLUMNS_FOR_FULL = [
    ("phone", "phone"),
    ("email", "email"),
    ("linkedin_url", "linkedin_url"),
    ("facebook_url", "facebook_url"),
    ("main_city", "main_city"),
    ("main_state", "main_state"),
    ("main_country", "main_country"),
    ("company_type", "profile_company_type"),
    ("working_industry", "working_industry"),
    ("employee_range", "profile_employee_range"),
    ("estimated_revenue", "estimated_revenue"),
    ("description", "description"),
    ("employee_names", "employee_names"),
    ("employee_job_titles", "employee_job_titles"),
    ("employee_emails", "employee_emails"),
    ("scrape_status", "profile_status"),
]
ENTITY_FULL_COLUMNS = ENTITY_SEARCH_COLUMNS + [out for _, out in PROFILE_COLUMNS_FOR_FULL]

WEBSITE_EXPORT_COLUMNS = ["company_name", "website"]

_FIRST_VALUE_FIELDS = (
    "company_id",
    "company_name",
    "website_url",
    "city",
    "state",
    "country",
    "grade",
    "employee_range",
    "founding_year",
    "key_takeaway",
    "description",
    "profile_url",
    "logo_url",
    "continent",
    "searched_country",
)


def country_export_dir(db: "CountryDatabase") -> Path:
    out_dir = OUTPUT_DIR / db.continent_code / country_slug(db.country_name)
    ensure_dirs(out_dir)
    return out_dir


def _entity_sort_key(entity_no: str) -> tuple[int, ...]:
    out: list[int] = []
    for piece in str(entity_no).split("."):
        out.append(int(piece) if piece.isdigit() else 10**6)
    return tuple(out)


def _write_csv(path: Path, columns: list[str], rows: Iterable[dict[str, Any]]) -> Path:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    os.replace(tmp, path)
    return path


def build_entity_rows(db: "CountryDatabase") -> list[dict[str, Any]]:
    """Aggregate search hits into one row per (entity, company)."""
    cursor = db.conn.execute(
        """
        SELECT
            te.category, te.entity_no, te.entity_name, te.list_id, te.position,
            te.sub_group, te.search_term AS entity_term, sr.term_key,
            sr.company_type_filter, sr.industry_filter,
            sr.company_id, sr.company_name, sr.website_url, sr.city, sr.state,
            sr.country, sr.grade, sr.employee_range, sr.founding_year,
            sr.key_takeaway, sr.description, sr.logo_url,
            COALESCE(NULLIF(cu.profile_url, ''), sr.profile_url) AS profile_url,
            sr.continent, sr.location_label AS searched_country
        FROM search_results sr
        JOIN term_entities te ON te.term_key = sr.term_key
        LEFT JOIN companies_unique cu ON cu.company_id = sr.company_id
        """
    )
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for r in cursor:
        company_key = r["company_id"] or (
            f"name:{r['company_name'].strip().lower()}" if r["company_name"] else ""
        )
        if not company_key:
            continue
        key = (r["entity_no"], company_key)
        g = groups.get(key)
        if g is None:
            g = {
                "category": r["category"],
                "entity_no": r["entity_no"],
                "entity_name": r["entity_name"],
                "_terms": {},
                "_sub_groups": [],
                "_types": set(),
                "_industries": set(),
            }
            groups[key] = g
        for field in _FIRST_VALUE_FIELDS:
            if not g.get(field) and r[field]:
                g[field] = r[field]
        order = (r["list_id"], r["position"])
        prev = g["_terms"].get(r["term_key"])
        if prev is None or order < prev[0]:
            g["_terms"][r["term_key"]] = (order, r["entity_term"])
        if r["sub_group"] and r["sub_group"] not in g["_sub_groups"]:
            g["_sub_groups"].append(r["sub_group"])
        if r["company_type_filter"]:
            g["_types"].add(COMPANY_TYPE_LABELS.get(r["company_type_filter"], r["company_type_filter"]))
        if r["industry_filter"]:
            g["_industries"].add(r["industry_filter"])

    rows: list[dict[str, Any]] = []
    for g in groups.values():
        terms = [t for _, t in sorted(g.pop("_terms").values(), key=lambda x: x[0])]
        g["search_terms"] = MULTI_SEP.join(terms)
        g["search_term_count"] = len(terms)
        g["sub_groups"] = MULTI_SEP.join(g.pop("_sub_groups"))
        g["company_types"] = MULTI_SEP.join(sorted(g.pop("_types")))
        g["industries"] = MULTI_SEP.join(sorted(g.pop("_industries")))
        g["employee_range"] = format_profile_csv_cell("employee_range", g.get("employee_range"))
        rows.append(g)
    rows.sort(
        key=lambda x: (
            _entity_sort_key(x["entity_no"]),
            str(x.get("company_name") or "").casefold(),
        )
    )
    return rows


def export_entity_search_csv(db: "CountryDatabase") -> Path | None:
    rows = build_entity_rows(db)
    if not rows:
        return None
    path = country_export_dir(db) / f"{country_slug(db.country_name)}_search_by_entity.csv"
    return _write_csv(path, ENTITY_SEARCH_COLUMNS, rows)


def export_entity_full_csv(db: "CountryDatabase") -> Path | None:
    rows = build_entity_rows(db)
    if not rows:
        return None
    profiles: dict[str, dict[str, Any]] = {}
    for p in db.conn.execute("SELECT * FROM company_profiles"):
        profiles[p["company_id"]] = dict(p)
    for row in rows:
        prof = profiles.get(row.get("company_id") or "")
        if prof and prof.get("website_url"):
            row["website_url"] = prof["website_url"]
        for src_col, out_col in PROFILE_COLUMNS_FOR_FULL:
            value = prof.get(src_col) if prof else None
            row[out_col] = format_profile_csv_cell(src_col, value)
    path = country_export_dir(db) / f"{country_slug(db.country_name)}_entity_full.csv"
    return _write_csv(path, ENTITY_FULL_COLUMNS, rows)


def export_company_profiles_only_csv(db: "CountryDatabase") -> Path | None:
    """Full company profile sheet: one row per company."""
    profiles = db.conn.execute("SELECT * FROM company_profiles ORDER BY company_name").fetchall()
    if not profiles:
        return None
    export_keys = [k for k in PROFILE_EXPORT_COLUMNS if k in profiles[0].keys()]
    path = country_export_dir(db) / f"{country_slug(db.country_name)}_company_profiles.csv"
    return _write_csv(
        path,
        export_keys,
        ({k: format_profile_csv_cell(k, row[k]) for k in export_keys} for row in profiles),
    )


def export_companies_websites_csv(db: "CountryDatabase") -> Path | None:
    """Company name and official website only."""
    rows = db.conn.execute(
        """
        SELECT
            cu.company_name,
            COALESCE(
                NULLIF(cp.website_url, ''),
                (SELECT sr.website_url FROM search_results sr
                 WHERE sr.company_id = cu.company_id AND COALESCE(sr.website_url, '') != ''
                 LIMIT 1),
                ''
            ) AS website
        FROM companies_unique cu
        LEFT JOIN company_profiles cp ON cu.company_id = cp.company_id
        ORDER BY cu.company_name COLLATE NOCASE
        """
    ).fetchall()
    if not rows:
        return None
    path = country_export_dir(db) / f"{country_slug(db.country_name)}_companies_websites.csv"
    return _write_csv(path, WEBSITE_EXPORT_COLUMNS, (dict(r) for r in rows))


def export_country_csv(db: "CountryDatabase") -> list[Path]:
    """Search-phase export (periodic + end of run)."""
    path = export_entity_search_csv(db)
    return [path] if path else []


def export_profile_csv(db: "CountryDatabase") -> list[Path]:
    """Profile-phase exports: profiles, websites, and entity rows with profile data."""
    paths: list[Path] = []
    for fn in (export_company_profiles_only_csv, export_companies_websites_csv, export_entity_full_csv):
        path = fn(db)
        if path:
            paths.append(path)
    return paths
