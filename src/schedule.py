"""Order in which (entity, term) pairs are searched within one country.

round_robin (default): round 1 = 1st term of every selected entity, round 2 =
  2nd term of every entity, ... If a run is stopped early, every entity has
  some data.
sequential: all terms of the first entity, then the next entity, ...
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from src.terms_loader import Entity, TermRow, ordered_terms


@dataclass(frozen=True)
class ScheduledTerm:
    index: int  # 1-based position in the whole schedule
    total: int
    round_index: int  # 0-based round (round_robin) or term position (sequential)
    max_rounds: int
    entity: Entity
    term: TermRow


def build_term_lists(
    entities: list[Entity],
    shuffle_key: str,
    terms_per_entity: int | None = None,
    term_order: str | None = None,
) -> list[tuple[Entity, list[TermRow]]]:
    out: list[tuple[Entity, list[TermRow]]] = []
    for entity in entities:
        terms = ordered_terms(entity, shuffle_key, term_order)
        if terms_per_entity:
            terms = terms[:terms_per_entity]
        if terms:
            out.append((entity, terms))
    return out


def iter_schedule(
    entities: list[Entity],
    shuffle_key: str,
    *,
    schedule: str = "round_robin",
    terms_per_entity: int | None = None,
    term_order: str | None = None,
) -> Iterator[ScheduledTerm]:
    lists = build_term_lists(entities, shuffle_key, terms_per_entity, term_order)
    if not lists:
        return
    total = sum(len(terms) for _, terms in lists)
    max_rounds = max(len(terms) for _, terms in lists)
    index = 0
    if schedule == "sequential":
        for entity, terms in lists:
            for pos, term in enumerate(terms):
                index += 1
                yield ScheduledTerm(index, total, pos, len(terms), entity, term)
        return
    for round_index in range(max_rounds):
        for entity, terms in lists:
            if round_index >= len(terms):
                continue
            index += 1
            yield ScheduledTerm(index, total, round_index, max_rounds, entity, terms[round_index])
