"""Search phase: continent -> country -> (round-robin) entity terms -> smart split.

For every scheduled (entity, term):
  * skip instantly if the term was already finished for this country (resume,
    or the same term listed under another entity);
  * otherwise walk the smart-split tree (see split_planner.py), where each node
    is one ensun search saved atomically to the country database.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from dataclasses import dataclass

from src.browser_errors import BrowserFetchError
from src.config_loader import (
    DATA_DIR,
    LOGS_DIR,
    country_slug,
    ensure_dirs,
    related_industries,
    settings,
)
from src.facets import Facets
from src.geo import Continent, Country
from src.schedule import ScheduledTerm, iter_schedule
from src.scraper_registry import ScraperStatusReporter
from src.session import BrowserSession, run_async
from src.shutdown import (
    ShutdownRequested,
    interruptible_sleep,
    keep_awake_disable,
    keep_awake_enable,
    register_shutdown_handler,
)
from src.split_planner import (
    ORDER_INDUSTRY_FIRST,
    STATUS_FAILED,
    NodeOutcome,
    SearchNode,
    SplitConfig,
    TermOutcome,
    missing_gap,
    needs_split,
    walk_term,
)
from src.storage.country_db import FINISHED_NODE_STATUSES, CountryDatabase
from src.storage.file_lock import FileLock, FileLockTimeout
from src.storage.run_durability import RunDurability, format_startup_banner
from src.terms_loader import Entity, TermList, load_term_list, parse_entity_selector

COMPANY_TYPE_LABELS = {
    "MANUFACTURER": "Manufacturer",
    "SERVICE_PROVIDER": "Service provider",
    "DISTRIBUTOR": "Distributor",
    "EDUCATIONAL_INSTITUTION": "Educational institution",
}


@dataclass
class SearchRunConfig:
    continent: Continent
    country: Country
    list_id: str
    entity_selector: str | None = None  # "all" | "1,3" | "1.1,2.7"
    force_rerun: bool = False
    terms_per_entity: int | None = None  # pilot: first N terms of each entity
    max_terms: int | None = None  # pilot: stop after N terms searched this run
    max_searches: int | None = None  # pilot: stop after N live ensun searches
    headless: bool = True
    keep_awake: bool = False


def split_config_from_settings() -> SplitConfig:
    cfg = settings()
    split = cfg.get("smart_split") or {}
    use_related = bool(split.get("use_related_industries", True))
    return SplitConfig(
        cap=int(cfg.get("max_companies_per_combo", 100)),
        max_depth=int(split.get("max_depth", 2)),
        fallback_company_types=tuple(split.get("fallback_company_types") or []),
        related_industries=tuple(related_industries()) if use_related else (),
        order=str(split.get("order", ORDER_INDUSTRY_FIRST)),
        min_gap=int(split.get("min_gap", 20)),
        done_tolerance=int(split.get("done_tolerance", 2)),
    )


# Technical detail: log file only.
_FILE_ONLY_PREFIXES = (
    "FAST:",
    "PAGINATE:",
    "WAIT: page_ready",
    "WAIT: pagination",
    "BROWSER_CHECK: ensun 429 check passed",
    "RETRY",
    "VPN_COOKIES: Opening tab",
    "VPN_COOKIES: No results yet",
    "VPN_COOKIES: Page load interrupted",
    "VPN_COOKIES: Page load failed",
    "VPN_COOKIES: Page did not load",
    "COOKIES: warm-up",
)


def friendly_browser_message(msg: str) -> str | None:
    """Plain-English screen version of a browser/connection message (None = file only).

    Unknown messages are shown unchanged, so nothing important is ever hidden.
    """
    if msg.startswith(_FILE_ONLY_PREFIXES):
        return None
    if msg.startswith(("BLOCKED:", "RATE_LIMIT:", "WAIT: Page not ready")):
        if "network" in msg.lower() or "ERR_" in msg:
            return "connection to ensun dropped -> getting a fresh connection"
        code = re.search(r"(?:page_status=|HTTP |http_)(\d{3})", msg)
        status = code.group(1) if code else ""
        reason = {
            "403": "ensun blocked this connection (403 Forbidden)",
            "429": "ensun says: too many requests (429)",
        }.get(status, f"ensun refused the page ({status or 'blocked'})")
        return f"{reason} -> getting a fresh connection"
    if msg.startswith("VPN_DEAD"):
        return "VPN: this server can't reach ensun -> switching to another one"
    if msg.startswith("VPN_WAIT"):
        m = re.search(r"in (\d+)s", msg)
        when = f" in {m.group(1)}s" if m else ""
        return f"VPN: still on the broken server - the VPN app allows a new one{when}, waiting..."
    if msg.startswith("VPN_ROTATE"):
        if "requesting server change" in msg:
            return "VPN: switching to a new server..."
        if "keeps giving back" in msg:
            return "VPN: the VPN app keeps giving the broken server - will try again shortly"
        m = re.search(r"->\s*[\d.]+\s*\(([\d.]+)s", msg)
        if m:
            return f"VPN: new server ready ({float(m.group(1)):.0f}s)"
        if "note" in msg:
            return None
        return msg  # failures / tool not running: show as-is
    if msg.startswith("VPN_COOKIES: Search results detected"):
        return "ensun works again -> continuing"
    if msg.startswith("BROWSER_CHECK:"):
        return "ensun's browser check did not pass -> getting a fresh connection"
    return msg


def _filter_name(node: SearchNode) -> str:
    """'Chemicals' / 'Chemicals > Manufacturer' — the filter of a search, in words."""
    parts: list[str] = []
    if node.industry:
        parts.append(node.industry)
    if node.company_type:
        parts.append(COMPANY_TYPE_LABELS.get(node.company_type, node.company_type))
    return " > ".join(parts) if parts else "no filter"


def _filter_text(node: SearchNode) -> str:
    parts: list[str] = []
    if node.company_type:
        parts.append(COMPANY_TYPE_LABELS.get(node.company_type, node.company_type))
    if node.industry:
        parts.append(node.industry)
    label = " x ".join(parts) if parts else "no filter"
    if node.expected_count is not None:
        label += f" ({node.expected_count:,})"
    return label


class _DbNodeRunner:
    """Runs one smart-split node: reuse a finished node from the DB, else search ensun."""

    def __init__(self, phase: "SearchPhaseRunner") -> None:
        self.phase = phase

    def _saved_short(self, row: dict) -> bool:
        """A stored search that collected fewer companies than ensun's count
        (pagination stalled) gets one more try in a later run."""
        if row["status"] != "completed" or row["facet_total"] is None:
            return False
        cfg = settings()
        expected = min(int(row["facet_total"]), self.phase.split_cfg.cap)
        tolerance = int(cfg.get("incomplete_results_tolerance", 2))
        max_runs = int(cfg.get("incomplete_rerun_max_runs", 2))
        return int(row["result_count"] or 0) < expected - tolerance and int(row["attempts"] or 0) < max_runs

    async def run(self, node: SearchNode) -> NodeOutcome:
        p = self.phase
        row = p.db.get_node(node.node_id)
        if (
            row
            and row["status"] in FINISHED_NODE_STATUSES
            and not p.config.force_rerun
            and not self._saved_short(row)
        ):
            keys = (
                p.db.node_company_keys(node.node_id)
                if row["status"] != "same_as_parent"
                else frozenset()
            )
            return NodeOutcome(
                status=row["status"],
                result_count=int(row["result_count"] or 0),
                facets=Facets.from_json(row["facets_json"]),
                searched=False,
                company_keys=keys,
            )
        return await p.search_node(node)

    def mark_same_as_parent(self, node: SearchNode, parent: NodeOutcome) -> NodeOutcome:
        self.phase.db.mark_node_same_as_parent(node, parent.facets, parent.result_count)
        return NodeOutcome(
            status="same_as_parent",
            result_count=parent.result_count,
            facets=parent.facets,
            searched=False,
            company_keys=parent.company_keys,
        )

    async def industry_counts(self, node: SearchNode, industries: list[str]) -> dict[str, int]:
        """Counts per related industry inside this company type (stored for resume)."""
        p = self.phase
        stored = {} if p.config.force_rerun else p.db.load_industry_counts(node.node_id)
        missing = [i for i in industries if i not in stored]
        if missing:
            fetched = await p.browser.count_results(
                node.search_term,
                p.config.continent.ensun_code,
                node.location_triple,
                node.company_type,
                missing,
            )
            stored.update(fetched)
            p.db.save_industry_counts(node, stored)
            p.log_industry_counts(node, stored, asked=len(missing))
        return {i: stored.get(i, 0) for i in industries}


class SearchPhaseRunner:
    def __init__(self, config: SearchRunConfig, term_list: TermList | None = None) -> None:
        self.config = config
        self.term_list = term_list or load_term_list(config.list_id)
        self.entities: list[Entity] = parse_entity_selector(self.term_list, config.entity_selector)
        self.db = CountryDatabase(config.continent.code, config.country.name)
        self.location_triple = config.country.ensun_location
        self.location_label = config.country.name
        self.split_cfg = split_config_from_settings()
        cfg = settings()
        self.delay = float(cfg.get("request_delay_seconds", 1.0))
        self.delay_empty = float(cfg.get("request_delay_seconds_empty", 0.4))
        self.schedule = str(cfg.get("search_schedule", "round_robin"))
        self.shuffle_key = f"{config.continent.code}:{config.country.name}"
        self.browser = BrowserSession()
        scrape_id = f"{config.continent.code}/{config.country.name}"
        self.browser.set_scrape_id(scrape_id)
        self.status_reporter = ScraperStatusReporter(scrape_id)
        self.browser.set_status_reporter(self.status_reporter)
        self._interrupted = False
        self._searches_this_run = 0
        self._terms_this_run = 0
        self._requeued_quietly = 0  # terms searched earlier, waiting for Pass 2
        self._node_runner = _DbNodeRunner(self)
        slug = country_slug(config.country.name)
        log_dir = LOGS_DIR / config.continent.code
        ensure_dirs(log_dir, DATA_DIR / config.continent.code)
        self.log_path = log_dir / f"{slug}_progress.log"
        self._run_lock_path = DATA_DIR / config.continent.code / f"{slug}.search.run.lock"
        self.durability: RunDurability | None = None

    # ------------------------------------------------------------------
    def _log(self, message: str, *, console: bool = True) -> None:
        """Write to the log file; also print it unless it is a technical detail."""
        if console:
            try:
                print(message, flush=True)
            except UnicodeEncodeError:
                enc = sys.stdout.encoding or "utf-8"
                print(message.encode(enc, errors="replace").decode(enc), flush=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(message + "\n")

    def _browser_log(self, msg: str) -> None:
        """Browser/connection messages: full text in the log file, plain words on screen."""
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(f"    {msg}\n")
        friendly = friendly_browser_message(msg)
        if friendly:
            try:
                print(f"    {friendly}", flush=True)
            except UnicodeEncodeError:
                print(f"    {friendly}".encode("ascii", "replace").decode(), flush=True)

    def _scheduled_terms(self) -> list[ScheduledTerm]:
        return list(
            iter_schedule(
                self.entities,
                self.shuffle_key,
                schedule=self.schedule,
                terms_per_entity=self.config.terms_per_entity,
            )
        )

    def prepare_run_summary(self) -> dict[str, object]:
        scheduled = self._scheduled_terms()
        unique_keys = {s.term.term_key for s in scheduled}
        done = self.db.load_done_term_keys(self.location_triple)
        stats = self.db.search_stats()
        return {
            "list_label": self.term_list.label,
            "entities": len(self.entities),
            "entities_total": len(self.term_list.entities),
            "scheduled_terms": len(scheduled),
            "unique_terms": len(unique_keys),
            "terms_already_done": len(unique_keys & done),
            "searches_done": stats["searches_done"],
            "unique_companies": self.db.unique_company_count(),
            "schedule": self.schedule,
        }

    def run(self) -> None:
        run_async(self._run_async())

    # ------------------------------------------------------------------
    async def _run_async(self) -> None:
        lock = FileLock(
            self._run_lock_path,
            timeout=float(settings().get("search_run_lock_timeout_seconds", 3)),
        )
        try:
            lock.acquire()
        except FileLockTimeout:
            self.db.close()
            raise SystemExit(
                f"Another search run is already active for {self.config.country.name} "
                f"(lock: {self._run_lock_path}). Stop it first, or pick another country."
            )
        try:
            await self._run_body()
        except (asyncio.CancelledError, ShutdownRequested, KeyboardInterrupt):
            self._interrupted = True
        finally:
            lock.release()

    async def _run_body(self) -> None:
        self.durability = RunDurability(self.db, self._log, mode="search")
        register_shutdown_handler(lambda: setattr(self, "_interrupted", True))
        keep_awake = self.config.keep_awake or bool(settings().get("keep_awake", False))
        if keep_awake:
            keep_awake_enable()
            self._log("Keep-awake enabled — PC will not sleep during this run.")

        self._log(format_startup_banner(self.db))
        reset = self.db.reset_in_progress_nodes()
        if reset:
            self._log(f"Recovered {reset} search(es) interrupted last time — they will be redone.")

        scheduled = self._scheduled_terms()
        mapped = self.db.save_term_entities(t for e in self.entities for t in e.terms)
        if self.config.force_rerun:
            cleared = self.db.clear_term_done(
                self.location_triple, {s.term.term_key for s in scheduled}
            )
            self._log(f"Force re-run: cleared {cleared} finished term marker(s).")
        done_terms = self.db.load_done_term_keys(self.location_triple)
        entity_labels = ", ".join(e.entity_no for e in self.entities)
        self._log(
            f"Target: {self.config.continent.code} -> {self.config.country.name} | "
            f"list={self.term_list.label} | entities={len(self.entities)} [{entity_labels}] | "
            f"schedule={self.schedule} | scheduled_terms={len(scheduled):,} | "
            f"term_entity_rows={mapped:,} | already_done_terms={len(done_terms):,} | "
            f"split={self.split_cfg.order}, min_gap {self.split_cfg.min_gap}, two_pass",
            console=False,
        )
        todo = len({s_.term.term_key for s_ in scheduled} - done_terms)
        self._log("")
        self._log(
            f"{self.config.country.name} | {self.term_list.label} | {len(self.entities)} entities | "
            f"{todo:,} terms to search ({len(done_terms):,} already done)"
        )

        self.status_reporter.set_message_provider(self._progress_message)
        skipped_pending = 0
        # A term whose search failed is retried on the NEXT run, not again for
        # every other entity that lists it in this run (each attempt already
        # went through the full retry + VPN recovery cycle).
        failed_this_run: set[str] = set()
        try:
            await self.status_reporter.start()
            await self.browser.start(headless=self.config.headless)
            self.browser.set_log_callback(self._browser_log)
            await self.browser.warm_up_cookies()
            two_pass = bool(settings().get("two_pass", True)) and self.split_cfg.max_depth > 0
            # term_key -> (scheduled item, companies still missing) for Pass 2
            pending: dict[str, tuple[ScheduledTerm, int]] = {}

            # ---- Pass 1: every term's no-filter search (or the full split if two_pass is off)
            for item in scheduled:
                if self._interrupted or self._limit_reached():
                    break
                key = item.term.term_key
                if key in done_terms:
                    skipped_pending += 1
                    continue
                if key in pending:
                    continue  # same term under another entity, already queued
                if key in failed_this_run:
                    self._log(
                        f"[{item.index:,}/{item.total:,}] {item.entity.entity_no} | "
                        f"term: \"{item.term.search_term}\" — failed earlier this run, retry next run"
                    )
                    continue
                if skipped_pending:
                    self._log(f"({skipped_pending:,} terms were already done - skipped)")
                    skipped_pending = 0
                if two_pass:
                    outcome, gap = await self._run_root(item)
                    if gap is not None:
                        pending[key] = (item, gap)
                        continue
                else:
                    outcome = await self._run_term(item)
                self._finish_term(item, outcome, done_terms, failed_this_run)
            if skipped_pending:
                self._log(f"({skipped_pending:,} terms were already done - skipped)")
            if self._requeued_quietly:
                self._log(
                    f"({self._requeued_quietly:,} term(s) searched in an earlier run are waiting "
                    "for Pass 2 filters)"
                )

            # ---- Pass 2: filters for the big terms, most companies missing first
            if pending and not (self._interrupted or self._limit_reached()):
                order = sorted(pending.values(), key=lambda x: -x[1])
                self._log(
                    f"\n===== PASS 2: using filters for {len(order):,} big term(s) "
                    f"(ensun has {self.split_cfg.min_gap}+ more companies than one search shows) - "
                    "biggest first ====="
                )
                for i, (item, gap) in enumerate(order, start=1):
                    if self._interrupted or self._limit_reached():
                        break
                    if item.term.term_key in failed_this_run:
                        continue
                    outcome = await self._run_term(item, pass2=(i, len(order), gap))
                    self._finish_term(item, outcome, done_terms, failed_this_run)
            elif pending:
                self._log(f"{len(pending):,} term(s) still need filters (Pass 2) — next run")
        finally:
            self.browser.set_log_callback(None)
            try:
                await self.browser.close_search_session(force_cookie_save=True)
                await self.browser.stop()
            except Exception as exc:
                self._log(f"Browser shutdown warning: {exc}")
            await self.status_reporter.stop("interrupted" if self._interrupted else "finished")
            self.durability.on_run_end()
            stats = self.db.search_stats()
            self._log("")
            self._log(
                f"{'STOPPED' if self._interrupted else 'FINISHED'}: "
                f"{self._terms_this_run:,} terms and {self._searches_this_run:,} searches this run | "
                f"{stats['terms_done']:,} terms done in total | "
                f"{self.db.unique_company_count():,} companies found"
            )
            if self._interrupted:
                self._log("Progress is saved. Run the same command again to resume.")
            if keep_awake:
                keep_awake_disable()
            self.db.close()

    def _limit_reached(self) -> bool:
        c = self.config
        if c.max_searches is not None and self._searches_this_run >= c.max_searches:
            return True
        if c.max_terms is not None and self._terms_this_run >= c.max_terms:
            return True
        return False

    def _should_stop_term(self) -> bool:
        if self._interrupted:
            return True
        c = self.config
        return c.max_searches is not None and self._searches_this_run >= c.max_searches

    def _progress_message(self) -> str:
        stats = self.db.search_stats()
        return (
            f"terms_done={stats['terms_done']} searches={stats['searches_done']} "
            f"unique={self.db.unique_company_count()}"
        )

    def _root(self, item: ScheduledTerm) -> SearchNode:
        return SearchNode(
            location_triple=self.location_triple,
            location_label=self.location_label,
            term_key=item.term.term_key,
            search_term=item.term.search_term,
        )

    def _log_term_header(self, item: ScheduledTerm, pass2: tuple[int, int, int] | None = None) -> None:
        self._term_start_total = self.db.unique_company_count()
        term = f"{item.entity.entity_no} {item.entity.entity_name} | \"{item.term.search_term}\""
        if pass2 is not None:
            i, n, gap = pass2
            self._log("")
            self._log(f"[Pass 2: {i:,} of {n:,}] {term} | ~{gap:,} more on ensun")
            return
        self._log("")
        self._log(f"[{item.index:,} of {item.total:,}] {term}")

    async def _run_root(self, item: ScheduledTerm) -> tuple[TermOutcome, int | None]:
        """Pass 1: only the no-filter search. Returns (outcome, gap); gap is set
        when the term should get filters in Pass 2 (enough companies missing)."""
        root = self._root(item)
        row = self.db.get_node(root.node_id)
        # Already searched in an earlier run (e.g. waiting for Pass 2): decide quietly.
        quiet = (
            row is not None
            and row["status"] in FINISHED_NODE_STATUSES
            and not self.config.force_rerun
            and not self._node_runner._saved_short(row)
        )
        if not quiet:
            self._log_term_header(item)
        result = TermOutcome()
        if self._should_stop_term():
            result.failed += 1
            return result, None
        outcome = await self._node_runner.run(root)
        if not quiet:
            self._on_node(root, outcome)
        result.nodes_total = 1
        result.node_ids.append(root.node_id)
        if outcome.searched:
            result.searches_run = 1
        elif outcome.finished:
            result.reused = 1
        if not outcome.finished:
            result.failed = 1
            return result, None
        if needs_split(outcome, self.split_cfg.cap, self.split_cfg.min_gap):
            gap = missing_gap(outcome)
            gap_value = gap if gap is not None else self.split_cfg.cap
            count = outcome.facets.total if outcome.facets is not None else None
            if quiet:
                self._requeued_quietly += 1
            else:
                self._log(f"    ~{gap_value:,} more on ensun -> will use filters in Pass 2")
            return result, gap_value
        if needs_split(outcome, self.split_cfg.cap) and not quiet:
            result.small_gap_skips = 1
            self._on_small_gap(root, outcome)
        return result, None

    async def _run_term(
        self, item: ScheduledTerm, pass2: tuple[int, int, int] | None = None
    ) -> TermOutcome:
        self._log_term_header(item, pass2)
        return await walk_term(
            self._root(item),
            self._node_runner,
            self.split_cfg,
            should_stop=self._should_stop_term,
            on_node=self._on_node,
            on_error=self._on_split_error,
            on_early_stop=self._on_early_stop,
            on_small_gap=self._on_small_gap,
        )

    def _finish_term(
        self,
        item: ScheduledTerm,
        outcome: TermOutcome,
        done_terms: set[str],
        failed_this_run: set[str],
    ) -> None:
        if outcome.complete and not self._interrupted:
            unique = self.db.mark_term_done(
                self.location_triple,
                item.term.term_key,
                item.term.search_term,
                nodes_total=outcome.nodes_total,
                searches_run=outcome.searches_run,
                capped_leaves=outcome.capped_leaves,
            )
            done_terms.add(item.term.term_key)
            self._terms_this_run += 1
            if outcome.nodes_total > 1:
                total = self.db.unique_company_count()
                new = total - getattr(self, "_term_start_total", total)
                limit = (
                    f" | {outcome.capped_leaves} search(es) hit ensun's 100 limit"
                    if outcome.capped_leaves
                    else ""
                )
                self._log(
                    f"    TERM DONE: {outcome.searches_run} search(es) | {unique:,} companies for this term"
                    f" | {new:,} new | total {total:,}{limit}"
                )
        elif not self._interrupted and not self._limit_reached():
            failed_this_run.add(item.term.term_key)
            self._log(
                f"    TERM NOT FINISHED: {outcome.failed} search(es) failed — retried next run"
            )

    def _on_small_gap(self, node: SearchNode, outcome: NodeOutcome) -> None:
        gap = missing_gap(outcome)
        indent = "    " + "  " * node.depth
        self._log(
            f"{indent}only {gap} more on ensun -> too few to be worth extra searches"
        )

    def _on_early_stop(self, node: SearchNode, found: int, skipped: int) -> None:
        indent = "    " + "  " * node.depth
        self._log(f"{indent}all companies ensun counted are found -> skipped {skipped} smaller search(es)")

    def _on_split_error(self, node: SearchNode, exc: Exception) -> None:
        indent = "    " + "  " * node.depth
        self._log(f"{indent}industry count check FAILED: {exc} -> this term is retried next run")

    def log_industry_counts(self, node: SearchNode, counts: dict[str, int], asked: int) -> None:
        with_cos = sorted(((i, n) for i, n in counts.items() if n > 0), key=lambda x: -x[1])
        shown = ", ".join(f"{i} {n:,}" for i, n in with_cos[:6])
        more = f", +{len(with_cos) - 6} more" if len(with_cos) > 6 else ""
        indent = "    " + "  " * node.depth
        self._log(
            f"{indent}more industries with companies (not in ensun's top 5): {shown}{more}"
        )

    def _on_node(self, node: SearchNode, outcome: NodeOutcome) -> None:
        indent = "    " + "  " * node.depth
        if outcome.status == "same_as_parent":
            self._log(f"{indent}{_filter_text(node)}: skipped (same companies as the search above)")
        elif not outcome.searched and outcome.finished:
            self._log(
                f"{indent}{_filter_text(node)}: already searched earlier ({outcome.result_count} companies)",
                console=False,
            )

    async def search_node(self, node: SearchNode) -> NodeOutcome:
        """Live ensun search for one node; always leaves the DB consistent."""
        assert self.durability is not None
        indent = "    " + "  " * node.depth
        self.db.mark_node_in_progress(node)
        self._searches_this_run += 1
        companies_count = 0
        started = time.monotonic()
        try:
            companies, pages_fetched, facets = await self.browser.fetch_search_pages(
                node.search_term,
                self.config.continent.ensun_code,
                node.location_triple,
                node.company_type,
                node.industry,
            )
            companies_count = len(companies)
            total_before = self.db.unique_company_count()
            status = self.db.save_node_results(
                node, companies, pages_fetched, facets, continent=self.config.continent.code
            )
            total_after = self.db.unique_company_count()
            self.durability.on_search_node_finished(
                node_id=node.node_id,
                status=status,
                search_term=node.search_term,
                company_type=node.company_type,
                industry=node.industry,
                depth=node.depth,
                location_triple=node.location_triple,
                result_count=companies_count,
                pages_fetched=pages_fetched,
                facet_total=facets.total if facets else None,
            )
            elapsed = time.monotonic() - started
            keys = frozenset(
                (c.get("company_id") or f"name:{(c.get('company_name') or '').strip().lower()}")
                for c in companies
                if c.get("company_id") or c.get("company_name")
            )
            outcome = NodeOutcome(status, companies_count, facets, searched=True, company_keys=keys)
            cap = self.split_cfg.cap
            dim = self.split_cfg.dimension(node.depth)
            label = f"{_filter_name(node)}: " if node.depth else ""
            found = (
                f"{companies_count:,} of {facets.total:,} found"
                if facets is not None
                else f"{companies_count:,} found (ensun count unknown)"
            )
            pages = f"{pages_fetched} page{'s' if pages_fetched != 1 else ''} in {elapsed:.0f}s"
            self._log(
                f"{indent}{label}{found} | {total_after - total_before:,} new | "
                f"total {total_after:,} | {pages}"
            )
            if node.depth and needs_split(outcome, cap, self.split_cfg.min_gap) and dim is not None:
                self._log(
                    f"{indent}  ~{missing_gap(outcome):,} more on ensun -> "
                    f"splitting by {dim.replace('_', ' ')}"
                )
            elif node.depth and facets is not None and facets.total > cap and dim is None:
                self._log(
                    f"{indent}  {facets.total - companies_count:,} more on ensun, but ensun shows "
                    f"only {cap} per search and there is no smaller filter left",
                    console=False,
                )
        except (asyncio.CancelledError, ShutdownRequested):
            raise
        except Exception as exc:
            self.db.mark_node_failed(node, str(exc))
            self.durability.on_search_node_finished(
                node_id=node.node_id,
                status="failed",
                search_term=node.search_term,
                company_type=node.company_type,
                industry=node.industry,
                depth=node.depth,
                location_triple=node.location_triple,
                error_message=str(exc)[:500],
            )
            diag = getattr(exc, "diagnostics", None) if isinstance(exc, BrowserFetchError) else None
            detail = f" [{diag.summary()}]" if diag else ""
            self._log(f"{indent}FAILED: {_filter_text(node)}: {exc}{detail}")
            outcome = NodeOutcome(STATUS_FAILED, 0, None, searched=True)
        await interruptible_sleep(self.delay_empty if companies_count == 0 else self.delay)
        return outcome
