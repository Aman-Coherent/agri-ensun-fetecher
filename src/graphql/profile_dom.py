"""Extract structured profile data from ensun company profile pages via DOM."""

from __future__ import annotations

import re
from typing import Any

from playwright.async_api import Page

from src.graphql.profile_normalize import (
    parse_countries_operating,
    parse_email,
    parse_employee_range,
    parse_employee_total,
    parse_founding_year,
    parse_labeled_line,
    parse_location_count,
    parse_location_line,
    parse_phone,
    split_specialised_areas,
)

SKIP_SECTIONS = (
    "Frequently asked questions",
    "FAQ",
    "Maximize your company research",
    "Search Pro",
    "This website stores data",
)

NOISE_LINES = frozenset(
    {
        "Claim this company",
        "Learn more",
        "Show employees >",
        "See 4 employees >",
        "View companies",
        "View details",
        "Find companies 10x faster with ensun",
        "Ask our AI to find the right companies for free",
        "Search",
        "25M+ companies",
        "250M+ products",
        "Free to use",
        "About",
        "Employees",
        "Overview",
        "ESG scoring",
        "Company lists",
        "Locations",
        "Certificates",
        "Grade (A-E)",
        "Accept all",
        "Allow necessary",
        "Customize",
    }
)


def _section_slice(text: str, start_label: str, end_labels: list[str]) -> str:
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


def _parse_key_value_block(block: str) -> dict[str, str]:
    lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
    out: dict[str, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if line in NOISE_LINES or line.startswith("---"):
            i += 1
            continue
        if i + 1 < len(lines):
            nxt = lines[i + 1]
            if nxt not in NOISE_LINES and not nxt.endswith("?"):
                out[line] = nxt
                i += 2
                continue
        i += 1
    return out


def _parse_certificates_from_body(body: str) -> list[dict[str, Any]]:
    """Fallback: parse certificate rows from Certificates section text."""
    idx = body.find("Certificates of")
    if idx < 0:
        return []
    chunk = body[idx:]
    for end in ("ESG score estimation", "Company lists", "Headquarter of", "Frequently asked"):
        pos = chunk.find(end)
        if pos > 0:
            chunk = chunk[:pos]
            break

    lines = [ln.strip() for ln in chunk.split("\n") if ln.strip()]
    skip = {"Name", "Status", "Description", "---"}
    data_lines = [ln for ln in lines if ln not in skip and not ln.startswith("Certificates of")]

    certs: list[dict[str, Any]] = []
    i = 0
    while i + 2 < len(data_lines):
        name, status, desc = data_lines[i], data_lines[i + 1], data_lines[i + 2]
        if status in ("Active", "Inactive", "Expired", "Pending") or len(name) < 80:
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


async def extract_profile_from_page(page: Page) -> dict[str, Any]:
    raw: dict[str, Any] = await page.evaluate(
        """
        () => {
          const bodyText = document.body.innerText || '';
          const h1 = document.querySelector('h1');
          const companyName = h1 ? h1.innerText.trim() : null;

          let description = '';
          if (h1) {
            let el = h1.nextElementSibling;
            while (el) {
              const tag = el.tagName;
              if (tag === 'H2' || tag === 'H3') break;
              const t = (el.innerText || '').trim();
              if (t && t.length > 80 && !t.includes('Claim this company')) {
                description = t;
                break;
              }
              el = el.nextElementSibling;
            }
          }

          const claimable = bodyText.includes('Claim this company');
          const managed = location.pathname.endsWith('-managed');

          const tel = document.querySelector('a[href^="tel:"]');
          const mail = document.querySelector('a[href^="mailto:"]');
          const phone = tel ? tel.getAttribute('href') : null;
          const email = mail ? mail.getAttribute('href') : null;

          let website = null;
          for (const a of document.querySelectorAll('a[href^="http"]')) {
            const href = a.href;
            if (!href.includes('ensun.io') && !href.includes('linkedin.com')
                && !href.includes('instagram.com') && !href.includes('facebook.com')) {
              website = href;
              break;
            }
          }

          const logo = document.querySelector('img[alt*="Logo"]');
          const logoUrl = logo ? logo.src : null;

          const employees = [];
          const empMatch = bodyText.match(/See\\s+(\\d+)\\s+employees?/i);
          const employeeTotal = empMatch ? parseInt(empMatch[1], 10) : null;

          const cards = document.querySelectorAll('[class*="MuiPaper"], [class*="mui-"]');
          for (const card of cards) {
            const t = card.innerText || '';
            if (t.includes('@') && t.length < 500) {
              const lines = t.split('\\n').map(s => s.trim()).filter(Boolean);
              if (lines.length >= 2) {
                employees.push({
                  full_name: lines[0],
                  job_title: lines[1] || null,
                  email: (t.match(/[\\w.+-]+@[\\w.-]+\\.\\w+/) || [])[0] || null,
                  linkedin_url: card.querySelector('a[href*="linkedin.com"]')?.href || null,
                });
              }
            }
          }

          const certRows = [];
          for (const table of document.querySelectorAll('table')) {
            const header = (table.innerText || '').slice(0, 80);
            if (header.includes('Name') && header.includes('Status')) {
              const rows = table.querySelectorAll('tr');
              for (const tr of rows) {
                const cells = Array.from(tr.querySelectorAll('td, th')).map(c => c.innerText.trim());
                if (cells.length >= 3 && cells[0] !== 'Name') {
                  certRows.push({
                    certificate_name: cells[0],
                    certificate_status: cells[1],
                    certificate_description: cells[2],
                  });
                }
              }
            }
          }

          const locRows = [];
          for (const table of document.querySelectorAll('table')) {
            const header = (table.innerText || '').slice(0, 60);
            if (header.includes('Location') && header.includes('Country') && header.includes('City')) {
              const rows = table.querySelectorAll('tr');
              for (const tr of rows) {
                const cells = Array.from(tr.querySelectorAll('td')).map(c => c.innerText.trim());
                if (cells.length >= 4) {
                  locRows.push({
                    location_role: cells[0],
                    country: cells[1],
                    state: cells[2],
                    city: cells[3],
                  });
                }
              }
            }
          }

          return {
            bodyText,
            companyName,
            description,
            claimable,
            managed,
            phone,
            email,
            website,
            logoUrl,
            employeeTotal,
            employees,
            certRows,
            locRows,
            pageUrl: location.href,
          };
        }
        """
    )

    body = raw.get("bodyText") or ""
    for skip in SKIP_SECTIONS:
        if skip in body:
            body = body.split(skip)[0]

    description = raw.get("description")
    if not description and raw.get("companyName"):
        name = raw.get("companyName")
        about_end = body.find("Claim this company")
        if about_end < 0:
            about_end = body.find("Contact information")
        chunk = body[:about_end] if about_end > 0 else body
        if name in chunk:
            desc = chunk.split(name, 1)[-1].strip()
            desc_lines = [
                ln for ln in desc.split("\n")
                if ln.strip() and ln.strip() not in NOISE_LINES and len(ln.strip()) > 60
            ]
            if desc_lines:
                description = desc_lines[0]

    overview_block = _section_slice(
        body,
        "Quick overview",
        [
            "Additional information",
            "Specialised areas",
            "ESG score estimation",
            "Company lists",
            "Headquarter of",
            "Locations of",
        ],
    )
    overview_lines = [
        ln.strip()
        for ln in overview_block.split("\n")
        if ln.strip() and ln.strip() not in NOISE_LINES
    ]

    overview_loc = parse_location_line(overview_lines[0] if overview_lines else None)
    founding_line = next((ln for ln in overview_lines if "Founded" in ln), None)
    employee_line = None
    for ln in overview_lines:
        if "Employee" in ln or re.match(r"[\d\-\+]+", ln):
            if parse_employee_range(ln):
                employee_line = ln
                break
    size_label = None
    for ln in overview_lines[1:]:
        if "Founded" not in ln and "Employee" not in ln and "," not in ln:
            if "Company" in ln or "Sized" in ln:
                size_label = ln
                break

    add_block = _section_slice(
        body,
        "Additional information",
        ["Specialised areas", "ESG score estimation", "Company lists", "Certificates of"],
    )
    add_kv = _parse_key_value_block(add_block)

    spec_block = _section_slice(
        body,
        "Specialised areas",
        ["Find companies 10x", "ESG score estimation", "Certificates of", "Company lists"],
    )
    spec_text = spec_block.replace("---", "").replace("\n", ", ")

    hq_block = _section_slice(
        body,
        "Headquarter of",
        ["Locations of", "Frequently asked", "Maximize your company"],
    )
    hq_city = parse_labeled_line(hq_block, "City")
    hq_state = parse_labeled_line(hq_block, "State")
    hq_country = parse_labeled_line(hq_block, "Country")
    countries_operating = parse_countries_operating(hq_block)

    contact_block = _section_slice(
        body,
        "Contact information",
        ["Find all employees", "Quick overview"],
    )
    phone = parse_phone(raw.get("phone") or contact_block)
    email = parse_email(raw.get("email") or contact_block)

    loc_rows = raw.get("locRows") or []
    if not loc_rows and hq_city:
        loc_rows = [
            {
                "location_role": "Headquarter",
                "country": hq_country,
                "state": hq_state,
                "city": hq_city,
            }
        ]

    employees_raw = raw.get("employees") or []
    employees: list[dict[str, Any]] = []
    seen_emp: set[str] = set()
    for emp in employees_raw:
        name = emp.get("full_name")
        if not name or name in seen_emp:
            continue
        seen_emp.add(name)
        employees.append(
            {
                "role_category": "Other",
                "full_name": name,
                "job_title": emp.get("job_title"),
                "email": emp.get("email"),
                "phone": None,
                "linkedin_url": emp.get("linkedin_url"),
                "profile_image_url": None,
            }
        )

    employee_total = raw.get("employeeTotal") or parse_employee_total(body)

    certificates = list(raw.get("certRows") or [])
    if not certificates:
        certificates = _parse_certificates_from_body(body)

    return {
        "company_name": raw.get("companyName"),
        "description": description,
        "website_url": raw.get("website"),
        "logo_url": raw.get("logoUrl"),
        "is_managed": 1 if raw.get("managed") else 0,
        "is_claimable": 1 if raw.get("claimable") else 0,
        "phone": phone,
        "email": email,
        "overview_city": overview_loc.get("city"),
        "overview_state": overview_loc.get("state"),
        "overview_country": overview_loc.get("country"),
        "founding_year": parse_founding_year(founding_line),
        "employee_range": parse_employee_range(employee_line),
        "company_size_label": size_label,
        "employee_total_visible": employee_total,
        "working_industry": add_kv.get("Working industry"),
        "estimated_revenue": add_kv.get("Estimated revenue"),
        "company_type": add_kv.get("Type of company"),
        "ownership_structure": add_kv.get("Ownership structure"),
        "location_count": parse_location_count(add_kv.get("Locations")),
        "location_count_label": add_kv.get("Locations"),
        "hq_city": hq_city,
        "hq_state": hq_state,
        "hq_country": hq_country,
        "countries_operating_count": countries_operating,
        "city": hq_city or overview_loc.get("city"),
        "state": hq_state or overview_loc.get("state"),
        "country": hq_country or overview_loc.get("country"),
        "specialised_areas": split_specialised_areas(spec_text),
        "locations": loc_rows,
        "certificates": certificates,
        "employees": employees,
        "profile_url": raw.get("pageUrl"),
        "dom_raw": {k: v for k, v in raw.items() if k != "bodyText"},
    }
