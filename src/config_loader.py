from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent
CONFIG_DIR = PROJECT_ROOT / "config"

# ENSUN_AGRI_RUNTIME_ROOT lets the test suite redirect every runtime folder
# (data/output/logs) into a throwaway temp dir so tests can never touch real
# scraped databases. Normal runs leave it unset.
_RUNTIME_ROOT = Path(os.environ.get("ENSUN_AGRI_RUNTIME_ROOT") or PROJECT_ROOT)
DATA_DIR = _RUNTIME_ROOT / "data"
OUTPUT_DIR = _RUNTIME_ROOT / "output"
LOGS_DIR = _RUNTIME_ROOT / "logs"
GRAPHQL_CAPTURED_DIR = _RUNTIME_ROOT / "graphql" / "captured"


def load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def settings() -> dict[str, Any]:
    return load_yaml("settings.yaml")


def geo_registry() -> dict[str, Any]:
    return load_yaml("geo_registry.yaml")


def related_industries() -> list[str]:
    """ensun industries searched when a company type still has >100 results."""
    return [str(x) for x in (load_yaml("related_industries.yaml").get("related_industries") or [])]


def search_terms_root() -> Path:
    """Folder holding 'List 1 - Entire Coverage' / 'List 2 - Top Search Terms'."""
    raw = str(settings().get("search_terms_root", "")).strip()
    if not raw:
        raise ValueError("settings.yaml: search_terms_root is not set")
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def country_slug(name: str) -> str:
    slug = name.lower().strip()
    slug = slug.replace("(", " ").replace(")", " ")
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    return slug.strip("_")


def country_output_dir(continent_code: str, country_name: str) -> Path:
    """Per-country export folder: output/{continent}/{country_slug}/."""
    return OUTPUT_DIR / continent_code / country_slug(country_name)


def term_key(term: str) -> str:
    """Normalised identity of a search term (case/whitespace-insensitive).

    ensun search is case-insensitive, so 'NPK fertilizer' and 'npk  fertilizer'
    are the same search and must share one result set.
    """
    return " ".join(term.split()).lower()


def node_id(
    location_triple: str,
    term_key_value: str,
    company_type: str = "",
    industry: str = "",
) -> str:
    """Stable id for one ensun search (term + location + optional filters).

    Deliberately excludes the entity: a term listed under several entities is
    the same ensun query, so it is searched once and credited to every entity
    that lists it (see export_entity_search_csv).
    """
    raw = "|".join([location_triple, term_key_value, company_type or "", industry or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def location_level(triple: str) -> str:
    parts = triple.split(",")
    if len(parts) != 3:
        return "country"
    _, state, city = parts
    if city and city.lower() != "null":
        return "city"
    if state and state.lower() != "null":
        return "state"
    return "country"


def ensure_dirs(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
