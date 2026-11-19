"""Load agri search-term lists (List 1 / List 2) grouped by category and entity.

Source files live under ``search_terms_root`` (settings.yaml), e.g.::

    Agro_Search_Terms/
      List 1 - Entire Coverage/0. ALL ENTITIES - List 1 (Master).csv
      List 2 - Top Search Terms/0. ALL ENTITIES - List 2 (Master).csv

Master CSV columns:
  List 1: Category, Entity #, Entity, Sub-group, Search Term
  List 2: Category, Entity #, Entity, Rank, Search Term
"""

from __future__ import annotations

import csv
import hashlib
import random
from dataclasses import dataclass, field
from pathlib import Path

from src.config_loader import (
    missing_search_terms_help,
    search_terms_root,
    settings,
    term_key,
)

REQUIRED_COLUMNS = ("Category", "Entity #", "Entity", "Search Term")


class SearchTermsNotFoundError(FileNotFoundError):
    """The master CSV is missing; the message tells the user where to put it."""


@dataclass(frozen=True)
class TermRow:
    list_id: str
    category: str
    entity_no: str
    entity_name: str
    search_term: str
    term_key: str
    position: int  # 0-based order within its entity (CSV order)
    sub_group: str | None = None
    rank: int | None = None


@dataclass
class Entity:
    list_id: str
    category: str
    entity_no: str
    entity_name: str
    terms: list[TermRow] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.entity_no} {self.entity_name}"


@dataclass
class TermList:
    list_id: str
    label: str
    path: Path
    entities: list[Entity]

    @property
    def term_count(self) -> int:
        return sum(len(e.terms) for e in self.entities)

    @property
    def unique_term_count(self) -> int:
        return len({t.term_key for e in self.entities for t in e.terms})

    def categories(self) -> list[str]:
        out: list[str] = []
        for e in self.entities:
            if e.category not in out:
                out.append(e.category)
        return out


def list_definitions() -> dict[str, dict[str, str]]:
    lists = settings().get("search_term_lists") or {}
    if not lists:
        raise ValueError("settings.yaml: search_term_lists is empty")
    return lists


def master_csv_path(list_id: str) -> Path:
    defs = list_definitions()
    if list_id not in defs:
        raise ValueError(f"Unknown term list {list_id!r}; choose from {sorted(defs)}")
    block = defs[list_id]
    return search_terms_root() / block["folder"] / block["master_file"]


def _entity_sort_key(entity_no: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in entity_no.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(10**6)
    return tuple(parts)


def load_term_list(list_id: str, path: Path | None = None) -> TermList:
    """Read a master CSV into ordered entities; duplicates within an entity are dropped."""
    csv_path = path or master_csv_path(list_id)
    if not csv_path.exists():
        message = f"Search term file not found: {csv_path}"
        if path is None:
            message += "\n\n" + missing_search_terms_help()
        raise SearchTermsNotFoundError(message)
    label = list_definitions().get(list_id, {}).get("label", list_id)

    entities: dict[str, Entity] = {}
    seen_in_entity: dict[str, set[str]] = {}
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{csv_path.name}: missing column(s) {missing}")
        for row in reader:
            term = " ".join((row.get("Search Term") or "").split())
            entity_no = (row.get("Entity #") or "").strip()
            if not term or not entity_no:
                continue
            key = term_key(term)
            entity = entities.get(entity_no)
            if entity is None:
                entity = Entity(
                    list_id=list_id,
                    category=(row.get("Category") or "").strip(),
                    entity_no=entity_no,
                    entity_name=(row.get("Entity") or "").strip(),
                )
                entities[entity_no] = entity
                seen_in_entity[entity_no] = set()
            if key in seen_in_entity[entity_no]:
                continue
            seen_in_entity[entity_no].add(key)
            rank_raw = (row.get("Rank") or "").strip()
            entity.terms.append(
                TermRow(
                    list_id=list_id,
                    category=entity.category,
                    entity_no=entity_no,
                    entity_name=entity.entity_name,
                    search_term=term,
                    term_key=key,
                    position=len(entity.terms),
                    sub_group=(row.get("Sub-group") or "").strip() or None,
                    rank=int(rank_raw) if rank_raw.isdigit() else None,
                )
            )

    ordered = sorted(entities.values(), key=lambda e: _entity_sort_key(e.entity_no))
    return TermList(list_id=list_id, label=label, path=csv_path, entities=ordered)


def parse_entity_selector(term_list: TermList, selector: str | None) -> list[Entity]:
    """Pick entities by a comma list of category numbers and/or entity numbers.

    "all" / "" -> every entity.  "1,3" -> categories 1 and 3.
    "1.1,2.7"  -> those two entities.  "1,2.7" -> all of category 1 plus 2.7.
    """
    if not selector or selector.strip().lower() in ("all", "*"):
        return list(term_list.entities)
    wanted_entities: set[str] = set()
    wanted_categories: set[str] = set()
    for token in selector.replace(" ", "").split(","):
        if not token:
            continue
        if "." in token:
            wanted_entities.add(token)
        else:
            wanted_categories.add(token)
    known_entities = {e.entity_no for e in term_list.entities}
    unknown = sorted(wanted_entities - known_entities)
    if unknown:
        raise ValueError(f"Unknown entity number(s): {', '.join(unknown)}")
    picked = [
        e
        for e in term_list.entities
        if e.entity_no in wanted_entities or e.entity_no.split(".")[0] in wanted_categories
    ]
    if not picked:
        raise ValueError(f"No entities matched {selector!r}")
    return picked


def _stable_seed(*parts: str) -> int:
    base = str(settings().get("term_shuffle_seed", 42))
    digest = hashlib.sha256("|".join([*parts, base]).encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def ordered_terms(entity: Entity, shuffle_key: str, term_order: str | None = None) -> list[TermRow]:
    """Terms of one entity in run order.

    csv    -> file order (List 2 = rank order, List 1 = sub-group order)
    random -> stable shuffle per country+entity (same order on every resume)
    """
    mode = term_order or str(settings().get("term_order", "csv"))
    terms = list(entity.terms)
    if mode == "random":
        rng = random.Random(_stable_seed(shuffle_key, entity.list_id, entity.entity_no))
        rng.shuffle(terms)
    return terms
