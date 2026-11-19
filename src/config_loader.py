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


def _configured_search_terms_root() -> Path:
    raw = str(settings().get("search_terms_root", "")).strip()
    if not raw:
        raise ValueError("settings.yaml: search_terms_root is not set")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def term_list_folder_names() -> list[str]:
    """Sub-folder names the search-terms folder must hold (from settings.yaml)."""
    lists = settings().get("search_term_lists") or {}
    return [
        str(block["folder"])
        for block in lists.values()
        if isinstance(block, dict) and block.get("folder")
    ]


def search_terms_candidates() -> list[Path]:
    """Every place the search-terms folder is looked for, in order.

    The configured path first; then a folder with the same name in the usual
    places a copied folder ends up on another PC (home, project, Desktop,
    Downloads, Documents, OneDrive), each also one level deeper because
    unzipping often adds an extra folder of the same name.
    """
    configured = _configured_search_terms_root()
    name = configured.name
    home = Path.home()
    bases = [
        home,
        PROJECT_ROOT,
        PROJECT_ROOT.parent,
        home / "Desktop",
        home / "Downloads",
        home / "Documents",
    ]
    try:
        onedrives = sorted(p for p in home.glob("OneDrive*") if p.is_dir())
    except OSError:
        onedrives = []
    for onedrive in onedrives:
        bases += [onedrive, onedrive / "Desktop", onedrive / "Documents"]

    candidates: list[Path] = []
    for folder in [configured, *(base / name for base in bases)]:
        for candidate in (folder, folder / name):
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def looks_like_search_terms_folder(folder: Path) -> bool:
    list_folders = term_list_folder_names()
    if not list_folders:
        return folder.is_dir()
    return any((folder / sub).is_dir() for sub in list_folders)


def search_terms_root() -> Path:
    """Folder holding 'List 1 - Entire Coverage' / 'List 2 - Top Search Terms'.

    The configured folder is used when it holds the lists. On another PC
    (different Windows user name, so ``C:/Users/<name>/...`` differs) the first
    candidate from ``search_terms_candidates`` that holds them is used, so
    settings.yaml never needs a per-PC edit (which would block ``git pull``).
    Nothing found -> the configured path (the caller reports it missing).
    """
    candidates = search_terms_candidates()
    for candidate in candidates:
        if looks_like_search_terms_folder(candidate):
            return candidate
    return candidates[0]


def missing_search_terms_help() -> str:
    """Plain-English help for when the search-terms folder can't be found."""
    configured = _configured_search_terms_root()
    target = Path.home() / configured.name
    subs = term_list_folder_names()
    lines = [
        f"The scraper needs the '{configured.name}' folder (copy it from the main PC).",
        f"Put it here:  {target}",
    ]
    if subs:
        lines.append(
            "Directly inside it there must be the folders: "
            + ", ".join(f"'{s}'" for s in subs)
        )
    lines.append("Places checked:")
    for candidate in search_terms_candidates():
        if not candidate.exists():
            state = "not found"
        elif looks_like_search_terms_folder(candidate):
            state = "found, but the list file inside is missing"
        else:
            state = "exists, but the list folders are not directly inside"
        lines.append(f"  - {candidate}  ({state})")
    return "\n".join(lines)


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
