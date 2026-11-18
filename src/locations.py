from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from src.config_loader import DATA_DIR, GRAPHQL_CAPTURED_DIR, country_slug, ensure_dirs
from src.geo import Country


@dataclass
class LocationEntry:
    triple: str
    label: str
    level: str
    parent_triple: str | None = None


@dataclass
class LocationRegistry:
    country: str
    continent: str
    ensun_country_triple: str
    extracted_at: str
    source: str
    entries: list[LocationEntry]

    def to_dict(self) -> dict[str, Any]:
        return {
            "country": self.country,
            "continent": self.continent,
            "ensun_country_triple": self.ensun_country_triple,
            "extracted_at": self.extracted_at,
            "source": self.source,
            "entries": [
                {
                    "triple": e.triple,
                    "label": e.label,
                    "level": e.level,
                    "parent_triple": e.parent_triple,
                }
                for e in self.entries
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LocationRegistry:
        entries = [
            LocationEntry(
                triple=e["triple"],
                label=e["label"],
                level=e["level"],
                parent_triple=e.get("parent_triple"),
            )
            for e in data.get("entries", [])
        ]
        return cls(
            country=data["country"],
            continent=data["continent"],
            ensun_country_triple=data["ensun_country_triple"],
            extracted_at=data.get("extracted_at", ""),
            source=data.get("source", "unknown"),
            entries=entries,
        )


def registry_path(country: Country) -> Path:
    ensure_dirs(DATA_DIR / "locations")
    return DATA_DIR / "locations" / f"{country_slug(country.name)}.json"


def load_registry(country: Country, continent_code: str) -> LocationRegistry | None:
    path = registry_path(country)
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        return LocationRegistry.from_dict(json.load(f))


def save_registry(registry: LocationRegistry) -> Path:
    path = registry_path(
        Country(
            name=registry.country,
            continent_code=registry.continent,
            ensun_location=registry.ensun_country_triple,
        )
    )
    with path.open("w", encoding="utf-8") as f:
        json.dump(registry.to_dict(), f, indent=2, ensure_ascii=False)
    return path


def default_registry(country: Country, continent_code: str) -> LocationRegistry:
    return LocationRegistry(
        country=country.name,
        continent=continent_code,
        ensun_country_triple=country.ensun_location,
        extracted_at=datetime.now(timezone.utc).isoformat(),
        source="default_country_only",
        entries=[
            LocationEntry(
                triple=country.ensun_location,
                label=country.name,
                level="country",
                parent_triple=None,
            )
        ],
    )


def get_or_create_registry(country: Country, continent_code: str) -> LocationRegistry:
    existing = load_registry(country, continent_code)
    if existing and existing.entries:
        return _ensure_parent_triples(existing)
    reg = default_registry(country, continent_code)
    save_registry(reg)
    return reg


def _ensure_parent_triples(registry: LocationRegistry) -> LocationRegistry:
    updated = False
    entries: list[LocationEntry] = []
    for entry in registry.entries:
        parent = entry.parent_triple if entry.parent_triple is not None else infer_parent_triple(entry.triple)
        if parent != entry.parent_triple:
            updated = True
        entries.append(
            LocationEntry(
                triple=entry.triple,
                label=entry.label,
                level=entry.level,
                parent_triple=parent,
            )
        )
    if updated:
        reg = LocationRegistry(
            country=registry.country,
            continent=registry.continent,
            ensun_country_triple=registry.ensun_country_triple,
            extracted_at=registry.extracted_at,
            source=registry.source,
            entries=entries,
        )
        save_registry(reg)
        return reg
    return registry


def locations_for_scrape(registry: LocationRegistry) -> list[tuple[str, str]]:
    """Primary scrape loop uses country-level only (avoids duplicate work per region)."""
    return [(e.triple, e.label) for e in registry.entries if e.level == "country"]


def merge_captured_locations(
    country: Country,
    continent_code: str,
    captured_entries: list[LocationEntry],
) -> LocationRegistry:
    base = get_or_create_registry(country, continent_code)
    seen = {e.triple for e in base.entries}
    entries = list(base.entries)
    for entry in captured_entries:
        if entry.level != "country" and not is_valid_drilldown_location(entry, country.name):
            continue
        parent = entry.parent_triple if entry.parent_triple is not None else infer_parent_triple(entry.triple)
        normalized = LocationEntry(
            triple=entry.triple,
            label=entry.label,
            level=entry.level,
            parent_triple=parent,
        )
        if normalized.triple in seen:
            continue
        seen.add(normalized.triple)
        entries.append(normalized)
    registry = LocationRegistry(
        country=country.name,
        continent=continent_code,
        ensun_country_triple=country.ensun_location,
        extracted_at=datetime.now(timezone.utc).isoformat(),
        source="graphql_capture",
        entries=entries,
    )
    save_registry(registry)
    return registry


def parse_location_triple(triple: str) -> tuple[str, str | None, str | None]:
    parts = triple.split(",")
    if len(parts) != 3:
        return triple, None, None
    country, state, city = parts
    state_val = None if state.lower() == "null" else state
    city_val = None if city.lower() == "null" else city
    return country, state_val, city_val


def triple_from_parts(country: str, state: str | None, city: str | None) -> str:
    return f"{country},{state or 'null'},{city or 'null'}"


def label_from_parts(country: str, state: str | None, city: str | None) -> str:
    if city:
        if state:
            return f"{city}, {state}, {country}"
        return f"{city}, {country}"
    if state:
        return f"{state}, {country}"
    return country


def infer_parent_triple(triple: str) -> str | None:
    country, state, city = parse_location_triple(triple)
    if city and state:
        return triple_from_parts(country, state, None)
    if state:
        return triple_from_parts(country, None, None)
    return None


def infer_level(country: str | None, state: str | None, city: str | None) -> str:
    if city:
        return "city"
    if state:
        return "state"
    return "country"


def is_valid_drilldown_location(entry: LocationEntry, country_name: str) -> bool:
    """Reject Ensun UI noise; keep plausible geographic state/city names."""
    if entry.level not in ("state", "city"):
        return False
    label = entry.label.split(",")[0].strip()
    _, state_part, _ = parse_location_triple(entry.triple)
    if state_part:
        label = state_part.strip()
    if not label or label.lower() == country_name.lower():
        return False
    block = {
        "products", "services", "core business", "search", "login", "register",
        "export", "key takeaways", "show", "register company", "manufacturer",
        "service provider", "distributor", "other", "packaging", "…",
    }
    lower = label.lower()
    if lower in block or any(lower.startswith(b) for b in block):
        return False
    if re.search(r"\(\d+\)", label):
        return False
    if re.fullmatch(r"\d+", label):
        return False
    if lower.startswith("show ") and "result" in lower:
        return False
    return True


def child_locations_for_drilldown(
    registry: LocationRegistry,
    parent_triple: str,
) -> list[LocationEntry]:
    """Return direct child locations one level below parent_triple."""
    country, state, city = parse_location_triple(parent_triple)
    if city:
        return []
    children: list[LocationEntry] = []
    for entry in registry.entries:
        if entry.triple == parent_triple:
            continue
        if entry.parent_triple == parent_triple:
            children.append(entry)
            continue
        c, s, ci = parse_location_triple(entry.triple)
        if city is None and state is None and s and not ci and c == country:
            if entry.level == "state" or (entry.level == "country" and entry.triple != parent_triple):
                if s:
                    children.append(entry)
        elif state and not city and ci and s == state and c == country:
            if entry.level == "city":
                children.append(entry)
    seen: set[str] = set()
    unique: list[LocationEntry] = []
    for entry in children:
        if entry.triple in seen:
            continue
        if not is_valid_drilldown_location(entry, registry.country):
            continue
        seen.add(entry.triple)
        unique.append(entry)
    return unique


def extract_locations_from_graphql_payload(data: Any) -> list[LocationEntry]:
    """Best-effort parse of location nodes from arbitrary GraphQL JSON."""
    entries: list[LocationEntry] = []
    seen: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            country = node.get("country") or node.get("countryName")
            state = node.get("state") or node.get("stateName") or node.get("region")
            city = node.get("city") or node.get("cityName")
            if country and any(k in node for k in ("country", "countryName")):
                triple = triple_from_parts(
                    str(country),
                    str(state) if state else None,
                    str(city) if city else None,
                )
                if triple not in seen:
                    seen.add(triple)
                    level = infer_level(
                        str(country) if country else None,
                        str(state) if state else None,
                        str(city) if city else None,
                    )
                    entries.append(
                        LocationEntry(
                            triple=triple,
                            label=label_from_parts(
                                str(country),
                                str(state) if state else None,
                                str(city) if city else None,
                            ),
                            level=level,
                            parent_triple=infer_parent_triple(triple),
                        )
                    )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return entries


def save_captured_graphql(name: str, payload: dict[str, Any], response: Any) -> None:
    ensure_dirs(GRAPHQL_CAPTURED_DIR)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = GRAPHQL_CAPTURED_DIR / f"{stamp}_{name}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump({"request": payload, "response": response}, f, ensure_ascii=False, indent=2)
