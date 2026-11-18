"""Flatten multi-value profile fields into single-row string columns."""

from __future__ import annotations

from typing import Any

# Simple name lists — comma-separated (values rarely contain commas)
LIST_SEP = ", "

# Parallel multi-field records (certificates, locations) — semicolon between records
RECORD_SEP = "; "

# Fields inside one location record
FIELD_SEP = " | "


def _join_simple(values: list[str | None]) -> str | None:
    cleaned = [v.strip() for v in values if v and str(v).strip()]
    if not cleaned:
        return None
    return LIST_SEP.join(cleaned)


def flatten_specialised_areas(areas: list[str] | None) -> str | None:
    return _join_simple(areas or [])


def flatten_company_lists(lists: list[dict[str, Any]] | None) -> str | None:
    names = [lst.get("list_name") for lst in (lists or []) if lst.get("list_name")]
    return _join_simple(names)


def flatten_locations(locations: list[dict[str, Any]] | None) -> str | None:
    """Additional locations only — HQ is stored in main_city/state/country columns."""
    if not locations:
        return None
    parts: list[str] = []
    for loc in locations:
        role = (loc.get("location_role") or "Location").strip()
        if role.lower() == "headquarter":
            continue
        city = loc.get("city") or ""
        state = loc.get("state") or ""
        country = loc.get("country") or ""
        parts.append(FIELD_SEP.join([role, city, state, country]))
    return RECORD_SEP.join(parts) if parts else None


def main_address_from_profile(profile: dict[str, Any]) -> dict[str, str | None]:
    """Headquarters / main address as separate fields."""
    return {
        "main_city": profile.get("city") or profile.get("hq_city") or profile.get("overview_city"),
        "main_state": profile.get("state") or profile.get("hq_state") or profile.get("overview_state"),
        "main_country": profile.get("country") or profile.get("hq_country") or profile.get("overview_country"),
    }


def flatten_certificates(certs: list[dict[str, Any]] | None) -> dict[str, str | None]:
    if not certs:
        return {
            "certificate_names": None,
            "certificate_statuses": None,
            "certificate_descriptions": None,
        }
    names: list[str] = []
    statuses: list[str] = []
    descriptions: list[str] = []
    for cert in certs:
        name = (cert.get("certificate_name") or "").strip()
        if not name:
            continue
        names.append(name)
        statuses.append((cert.get("certificate_status") or "").strip())
        desc = (cert.get("certificate_description") or "").strip()
        descriptions.append(desc.replace(RECORD_SEP, " ").replace("\n", " "))
    return {
        "certificate_names": RECORD_SEP.join(names) if names else None,
        "certificate_statuses": RECORD_SEP.join(statuses) if statuses else None,
        "certificate_descriptions": RECORD_SEP.join(descriptions) if descriptions else None,
    }


def flatten_employees(employees: list[dict[str, Any]] | None) -> dict[str, str | None]:
    if not employees:
        return {
            "employee_names": None,
            "employee_job_titles": None,
            "employee_role_categories": None,
            "employee_emails": None,
            "employee_linkedin_urls": None,
        }
    names: list[str] = []
    titles: list[str] = []
    roles: list[str] = []
    emails: list[str] = []
    linkedins: list[str] = []
    for emp in employees:
        name = (emp.get("full_name") or "").strip()
        if not name:
            continue
        names.append(name)
        titles.append((emp.get("job_title") or "").strip())
        roles.append((emp.get("role_category") or "").strip())
        emails.append((emp.get("email") or "").strip())
        linkedins.append((emp.get("linkedin_url") or "").strip())
    return {
        "employee_names": _join_simple(names),
        "employee_job_titles": _join_simple(titles),
        "employee_role_categories": _join_simple(roles),
        "employee_emails": _join_simple(emails),
        "employee_linkedin_urls": _join_simple(linkedins),
    }


def flatten_profile_record(profile: dict[str, Any]) -> dict[str, Any]:
    """Add flat string columns to profile dict (one row per company)."""
    flat: dict[str, Any] = {}
    flat.update(main_address_from_profile(profile))
    flat["specialised_areas"] = flatten_specialised_areas(profile.get("specialised_areas"))
    flat["location_entries"] = flatten_locations(profile.get("locations"))
    flat.update(flatten_certificates(profile.get("certificates")))
    flat.update(flatten_employees(profile.get("employees")))
    for key in ("linkedin_url", "facebook_url", "twitter_url", "google_maps_url"):
        flat[key] = profile.get(key)
    return flat


# Column order for the single export sheet (grouped, not mixable)
PROFILE_EXPORT_COLUMNS = [
    "company_id",
    "company_name",
    "profile_url",
    "description",
    "website_url",
    "logo_url",
    "phone",
    "email",
    "main_city",
    "main_state",
    "main_country",
    "linkedin_url",
    "facebook_url",
    "twitter_url",
    "google_maps_url",
    "founding_year",
    "employee_range",
    "employee_total_visible",
    "company_size_label",
    "working_industry",
    "estimated_revenue",
    "company_type",
    "ownership_structure",
    "location_count",
    "countries_operating_count",
    "specialised_areas",
    "location_entries",
    "certificate_names",
    "certificate_statuses",
    "certificate_descriptions",
    "employee_names",
    "employee_job_titles",
    "employee_role_categories",
    "employee_emails",
    "employee_linkedin_urls",
    "is_managed",
    "is_claimable",
    "scrape_status",
    "scraped_at",
    "data_source",
]


def format_profile_csv_cell(column: str, value: Any) -> Any:
    """Format CSV cells so Excel does not auto-convert employee bands to dates."""
    if column != "employee_range" or value in (None, ""):
        return value
    text = str(value).strip()
    if not text:
        return value
    # Match Ensun label; leading tab forces text when opening CSV in Excel.
    if not text.lower().endswith("employees"):
        text = f"{text} Employees"
    return f"\t{text}"
