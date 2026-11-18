"""Smart split: as many companies as possible per term, with as few page requests as possible.

ensun (free account) shows at most 100 companies per search. Decisions are
driven by the GAP = ensun's count for a search minus the companies we already
have for it — page requests are the real cost (~1-8 s each), new companies the
value. Data behind each rule (2026-11-11 UAE/India runs) is in the README.

order = "industry_first" (default):
  depth 0  term + country, no filter. Gap < min_gap -> done.
  depth 1  one search per RELATED industry (config/related_industries.yaml) that
           has companies; counts come from ensun's quick count query, so empty
           industries cost no page load.
  depth 2  an industry whose own gap is still >= min_gap -> one search per
           company type ensun lists inside it.
order = "type_first" (the earlier method): company type at depth 1, industry at depth 2.

Within a split, children run largest first and the split STOPS as soon as the
branch holds (almost) every company ensun counted — e.g. a group of 102 where
the first search already returned 100 is not split into 20 tiny searches.

A child whose count equals its parent's total returns the same result set
(the filter narrows nothing): recorded as ``same_as_parent`` without a search.

Pure logic; the runner supplies a NodeRunner that does the actual fetch (or
loads an already-finished node from the database on resume).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

from src.config_loader import node_id
from src.facets import Facets

STATUS_COMPLETED = "completed"
STATUS_EMPTY = "empty"
STATUS_SAME_AS_PARENT = "same_as_parent"
STATUS_FAILED = "failed"
FINISHED_NODE_STATUSES = frozenset({STATUS_COMPLETED, STATUS_EMPTY, STATUS_SAME_AS_PARENT})

ORDER_INDUSTRY_FIRST = "industry_first"
ORDER_TYPE_FIRST = "type_first"


@dataclass(frozen=True)
class SearchNode:
    location_triple: str
    location_label: str
    term_key: str
    search_term: str
    company_type: str = ""
    industry: str = ""
    depth: int = 0
    parent_id: str | None = None
    expected_count: int | None = None  # count ensun reported for this filter

    @property
    def node_id(self) -> str:
        return node_id(self.location_triple, self.term_key, self.company_type, self.industry)

    def filter_label(self) -> str:
        parts = [p for p in (self.company_type, self.industry) if p]
        return " x ".join(parts) if parts else "no filter"


@dataclass(frozen=True)
class NodeOutcome:
    status: str
    result_count: int = 0
    facets: Facets | None = None
    searched: bool = False  # True only when a live ensun search ran this time
    company_keys: frozenset[str] = frozenset()  # companies this search returned

    @property
    def finished(self) -> bool:
        return self.status in FINISHED_NODE_STATUSES

    @property
    def found(self) -> int:
        return len(self.company_keys) if self.company_keys else self.result_count


@dataclass
class TermOutcome:
    nodes_total: int = 0
    searches_run: int = 0
    reused: int = 0  # finished earlier (resume)
    same_as_parent: int = 0
    failed: int = 0
    capped_leaves: int = 0  # still > cap at the deepest level (ensun hides the rest)
    small_gap_skips: int = 0  # splits not worth it (fewer than min_gap companies missing)
    early_stops: int = 0  # splits stopped because all counted companies were found
    node_ids: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.failed == 0


@dataclass(frozen=True)
class SplitConfig:
    cap: int = 100
    max_depth: int = 2
    fallback_company_types: tuple[str, ...] = ()
    # Industries searched in the industry step. Empty = use ensun's visible top 5.
    related_industries: tuple[str, ...] = ()
    order: str = ORDER_TYPE_FIRST
    min_gap: int = 0  # split only if at least this many companies are still missing
    done_tolerance: int = 2  # a branch holding count - N companies counts as complete

    def dimension(self, depth: int) -> str | None:
        """Which filter the children of a node at ``depth`` use."""
        if depth >= self.max_depth:
            return None
        first, second = (
            ("industry", "company_type") if self.order == ORDER_INDUSTRY_FIRST else ("company_type", "industry")
        )
        return first if depth == 0 else second if depth == 1 else None


class NodeRunner(Protocol):
    async def run(self, node: SearchNode) -> NodeOutcome: ...

    def mark_same_as_parent(self, node: SearchNode, parent: NodeOutcome) -> NodeOutcome: ...

    async def industry_counts(
        self, node: SearchNode, industries: list[str]
    ) -> dict[str, int]: ...


def missing_gap(outcome: NodeOutcome) -> int | None:
    """Companies ensun counted for this search that we haven't got (None = unknown)."""
    if outcome.facets is None:
        return None
    return max(0, outcome.facets.total - outcome.found)


def needs_split(outcome: NodeOutcome, cap: int, min_gap: int = 0) -> bool:
    """Is a narrower set of searches worth it for this node?

    With ensun's count: only if more than ``cap`` exist AND at least ``min_gap``
    are still missing (count 102 / got 100 -> not worth ~20 searches). Facet
    counts stay authoritative: a search whose paging stalled at 59 of 1,621
    still splits. Without a count, a full page set is the only signal.
    """
    if not outcome.finished:
        return False
    if outcome.facets is not None:
        gap = missing_gap(outcome) or 0
        return outcome.facets.total > cap and gap >= max(1, min_gap)
    return outcome.result_count >= cap


def _child(parent: SearchNode, company_type: str, industry: str, count: int | None) -> SearchNode:
    return SearchNode(
        location_triple=parent.location_triple,
        location_label=parent.location_label,
        term_key=parent.term_key,
        search_term=parent.search_term,
        company_type=company_type,
        industry=industry,
        depth=parent.depth + 1,
        parent_id=parent.node_id,
        expected_count=count,
    )


def company_type_children(parent: SearchNode, outcome: NodeOutcome, cfg: SplitConfig) -> list[SearchNode]:
    if outcome.facets is not None:
        options: list[tuple[str, int | None]] = [(c, n) for c, n in outcome.facets.categories if n > 0]
    else:
        options = [(c, None) for c in cfg.fallback_company_types]
    options.sort(key=lambda x: -(x[1] or 0))
    return [_child(parent, name, parent.industry, count) for name, count in options]


def known_industry_counts(outcome: NodeOutcome, related: tuple[str, ...]) -> dict[str, int]:
    """Counts ensun already showed in the parent's filter panel (its top 5)."""
    if outcome.facets is None:
        return {}
    wanted = set(related)
    return {name: n for name, n in outcome.facets.industries if name in wanted}


def industry_children(
    parent: SearchNode,
    outcome: NodeOutcome,
    cfg: SplitConfig,
    counts: dict[str, int] | None = None,
) -> list[SearchNode]:
    """Industry searches under this node, largest first, skipping empty ones.

    With related_industries configured, ``counts`` must hold a count per related
    industry; otherwise ensun's visible top-5 industries are used.
    """
    if cfg.related_industries:
        counts = counts or {}
        options = [(name, counts.get(name, 0)) for name in cfg.related_industries]
    elif outcome.facets is not None:
        options = list(outcome.facets.industries)
    else:
        options = []
    options = [(name, n) for name, n in options if n and n > 0]
    options.sort(key=lambda x: -x[1])
    return [_child(parent, parent.company_type, name, n) for name, n in options]


def child_nodes(
    parent: SearchNode,
    parent_outcome: NodeOutcome,
    cfg: SplitConfig,
    industry_counts: dict[str, int] | None = None,
) -> list[SearchNode]:
    """Narrower searches for a node that needs splitting (largest first)."""
    dim = cfg.dimension(parent.depth)
    if dim == "company_type":
        return company_type_children(parent, parent_outcome, cfg)
    if dim == "industry":
        return industry_children(parent, parent_outcome, cfg, industry_counts)
    return []


def is_same_as_parent(node: SearchNode, parent_outcome: NodeOutcome) -> bool:
    """Filter narrows nothing: the child's count equals the parent's total."""
    if node.expected_count is None or parent_outcome.facets is None:
        return False
    return node.expected_count == parent_outcome.facets.total


async def walk_term(
    root: SearchNode,
    runner: NodeRunner,
    cfg: SplitConfig,
    *,
    should_stop: Callable[[], bool] | None = None,
    on_node: Callable[[SearchNode, NodeOutcome], Awaitable[None] | None] | None = None,
    on_error: Callable[[SearchNode, Exception], None] | None = None,
    on_early_stop: Callable[[SearchNode, int, int], None] | None = None,
    on_small_gap: Callable[[SearchNode, NodeOutcome], None] | None = None,
) -> TermOutcome:
    """Depth-first smart split for one term. Resumable: the runner returns
    stored outcomes (and stored industry counts) for work finished earlier."""
    result = TermOutcome()

    async def visit(node: SearchNode, parent_outcome: NodeOutcome | None) -> set[str]:
        """Search ``node`` (and its children if worth it); returns the company keys found."""
        if should_stop and should_stop():
            result.failed += 1  # unfinished -> term must not be marked done
            return set()
        if parent_outcome is not None and is_same_as_parent(node, parent_outcome):
            outcome = runner.mark_same_as_parent(node, parent_outcome)
            result.same_as_parent += 1
        else:
            outcome = await runner.run(node)
            if outcome.searched:
                result.searches_run += 1
            elif outcome.finished:
                result.reused += 1
        result.nodes_total += 1
        result.node_ids.append(node.node_id)
        if on_node is not None:
            maybe = on_node(node, outcome)
            if maybe is not None:
                await maybe
        found: set[str] = set(outcome.company_keys)
        if not outcome.finished:
            result.failed += 1
            return found
        if not needs_split(outcome, cfg.cap):
            return found  # ensun has <= cap: this search got everything
        if not needs_split(outcome, cfg.cap, cfg.min_gap):
            result.small_gap_skips += 1
            if on_small_gap is not None:
                on_small_gap(node, outcome)
            return found
        dim = cfg.dimension(node.depth)
        if dim is None:
            result.capped_leaves += 1
            return found
        counts: dict[str, int] | None = None
        if dim == "industry" and cfg.related_industries:
            counts = known_industry_counts(outcome, cfg.related_industries)
            missing = [i for i in cfg.related_industries if i not in counts]
            try:
                if missing:
                    counts.update(await runner.industry_counts(node, missing))
            except Exception as exc:  # count check failed after recovery -> retry next run
                result.failed += 1
                if on_error is not None:
                    on_error(node, exc)
                return found
        children = child_nodes(node, outcome, cfg, counts)
        if not children:
            result.capped_leaves += 1
            return found
        target = outcome.facets.total - cfg.done_tolerance if outcome.facets is not None else None
        for i, child in enumerate(children):
            if target is not None and found and len(found) >= target:
                result.early_stops += 1
                if on_early_stop is not None:
                    on_early_stop(node, len(found), len(children) - i)
                break
            found |= await visit(child, outcome)
        return found

    await visit(root, None)
    return result
