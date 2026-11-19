from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as python -m src.cli from project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:  # Windows consoles: print accents (Côte d'Ivoire) instead of crashing
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

from src.config_loader import country_slug, settings
from src.geo import (
    Continent,
    Country,
    find_continent,
    find_country,
    load_continents,
    pick_continent_interactive,
    pick_country_interactive,
)
from src.progress import print_profile_progress, print_progress
from src.terms_loader import TermList, list_definitions, load_term_list, parse_entity_selector

LIST_ALIASES = {"1": "list1", "2": "list2", "list1": "list1", "list2": "list2"}


# ----------------------------------------------------------------------
# Shared prompts / resolution
# ----------------------------------------------------------------------

def _resolve_keep_awake(interactive: bool) -> bool:
    """Interactive runs keep the PC awake by default (overnight-safe)."""
    if interactive:
        return bool(settings().get("keep_awake_interactive", True))
    return bool(settings().get("keep_awake", False))


def _resolve_target(args: argparse.Namespace) -> tuple[Continent, Country, bool]:
    """(continent, country, interactive) from flags or menus."""
    if getattr(args, "continent", None) and getattr(args, "country", None):
        continent = find_continent(args.continent)
        if not continent:
            codes = ", ".join(c.code for c in load_continents())
            raise SystemExit(f"Unknown continent: {args.continent} (choose from {codes})")
        country = find_country(continent.code, args.country)
        if not country:
            names = ", ".join(c.name for c in continent.countries)
            raise SystemExit(f"Unknown country: {args.country} in {continent.code} ({names})")
        return continent, country, False
    continent = pick_continent_interactive()
    country = pick_country_interactive(continent)
    return continent, country, True


def _pick_list_interactive() -> str:
    defs = list_definitions()
    ids = list(defs)
    print("\nWhich search-term list?")
    for idx, list_id in enumerate(ids, start=1):
        print(f"  {idx}. {defs[list_id].get('label', list_id)}")
    while True:
        choice = input("\nEnter number: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(ids):
            return ids[int(choice) - 1]
        print("Invalid choice. Try again.")


def _print_entities(term_list: TermList) -> None:
    current = None
    for e in term_list.entities:
        if e.category != current:
            current = e.category
            print(f"\n  {current}")
        print(f"    {e.entity_no:>4}  {e.entity_name}  ({len(e.terms)} terms)")


def _pick_entities_interactive(term_list: TermList) -> str:
    print(
        f"\n{term_list.label}: {len(term_list.entities)} entities, "
        f"{term_list.term_count:,} terms ({term_list.unique_term_count:,} unique)"
    )
    show = input("Show the entity list? [y/N]: ").strip().lower()
    if show in ("y", "yes"):
        _print_entities(term_list)
    print(
        "\nWhich entities to search?"
        "\n  Enter       = all entities"
        "\n  1,3         = whole categories 1 and 3"
        "\n  1.1,2.7     = just those entities"
        "\n  1,2.7       = all of category 1 plus entity 2.7"
    )
    while True:
        raw = input("\nEntities [all]: ").strip()
        try:
            picked = parse_entity_selector(term_list, raw or "all")
        except ValueError as exc:
            print(f"  {exc}. Try again.")
            continue
        print(f"  -> {len(picked)} entities, {sum(len(e.terms) for e in picked):,} terms")
        return raw or "all"


def _resolve_list_id(raw: str | None, interactive: bool) -> str:
    if raw:
        list_id = LIST_ALIASES.get(raw.strip().lower())
        if not list_id:
            raise SystemExit(f"Unknown --list {raw!r}; use 1 or 2")
        return list_id
    if interactive:
        return _pick_list_interactive()
    raise SystemExit("--list is required with --continent/--country (use 1 or 2)")


# ----------------------------------------------------------------------
# search
# ----------------------------------------------------------------------

def cmd_search(args: argparse.Namespace) -> None:
    from src.search_phase import SearchPhaseRunner, SearchRunConfig
    from src.storage.run_durability import format_startup_banner

    continent, country, interactive = _resolve_target(args)
    list_id = _resolve_list_id(args.list, interactive)
    term_list = load_term_list(list_id)
    if args.entities is not None:
        selector = args.entities
        try:
            parse_entity_selector(term_list, selector)
        except ValueError as exc:
            raise SystemExit(f"--entities: {exc}. See: python -m src.cli terms --list {list_id[-1]}")
    elif interactive:
        selector = _pick_entities_interactive(term_list)
    else:
        selector = "all"

    slug = country_slug(country.name)
    print(f"\nTarget: {continent.code} -> {country.name}")
    print(f"Database: data/{continent.code}/{slug}.db")
    keep_awake = _resolve_keep_awake(interactive)
    runner = SearchPhaseRunner(
        SearchRunConfig(
            continent=continent,
            country=country,
            list_id=list_id,
            entity_selector=selector,
            force_rerun=args.force_rerun,
            terms_per_entity=args.terms_per_entity,
            max_terms=args.max_terms,
            max_searches=args.max_searches,
            headless=not args.show_browser,
            keep_awake=keep_awake,
        ),
        term_list=term_list,
    )
    summary = runner.prepare_run_summary()
    print("\nTarget ready:")
    print(format_startup_banner(runner.db))
    print(f"  List          : {summary['list_label']}")
    print(f"  Entities      : {summary['entities']} of {summary['entities_total']}")
    print(f"  Order         : {summary['schedule']}")
    print(
        f"  Terms         : {summary['scheduled_terms']:,} scheduled "
        f"({summary['unique_terms']:,} unique — a term shared by several entities is searched once)"
    )
    print(f"  Already done  : {summary['terms_already_done']:,} terms")
    print(
        "  Filters       : smart split — 1 search if <=100 companies, "
        "else by company type, then by industry"
    )
    if keep_awake:
        print("  Keep-awake    : on (PC will not sleep during this run)")
    if interactive:
        answer = input("\nStart scraping? [Y/n]: ").strip().lower()
        if answer in ("n", "no"):
            runner.db.close()
            raise SystemExit("Cancelled.")
    else:
        print("\nStarting scrape...")
    runner.run()


# ----------------------------------------------------------------------
# profile (unchanged behaviour from the nutraceutical scraper)
# ----------------------------------------------------------------------

def _prompt_profile_workers(interactive: bool, cli_workers: int | None) -> int:
    cfg = settings()
    max_workers = int(cfg.get("profile_max_workers", 24))
    default = int(cfg.get("profile_default_workers", 1))
    if cli_workers is not None:
        return max(1, min(cli_workers, max_workers))
    if not interactive:
        return default
    while True:
        raw = input(
            f"\nNumber of profile workers [1-{max_workers}] (default {default}): "
        ).strip()
        if not raw:
            return default
        try:
            n = int(raw)
            if 1 <= n <= max_workers:
                return n
        except ValueError:
            pass
        print(f"Enter an integer from 1 to {max_workers}.")


def cmd_profile(args: argparse.Namespace) -> None:
    from src.profile_phase import ProfilePhaseRunner, ProfileRunConfig
    from src.storage.run_durability import format_profile_startup_banner

    continent, country, interactive = _resolve_target(args)
    slug = country_slug(country.name)
    print(f"\nProfile target: {continent.code} -> {country.name}")
    print(f"Database: data/{continent.code}/{slug}.db")
    print("Opening database... (large countries may take 30-90 seconds)", flush=True)

    keep_awake = _resolve_keep_awake(interactive)
    worker_count = _prompt_profile_workers(interactive, getattr(args, "workers", None))
    runner = ProfilePhaseRunner(
        ProfileRunConfig(
            continent=continent,
            country=country,
            force_rerun=args.force_rerun,
            limit=args.limit,
            headless=not args.show_browser,
            keep_awake=keep_awake,
            worker_count=worker_count,
        )
    )
    summary = runner.prepare_run_summary()
    secs_per = float(settings().get("profile_estimated_seconds_per_profile", 10))
    est_min = (int(summary["pending"]) * secs_per) / 60 / max(1, worker_count)
    print("\nProfile scrape ready:")
    print(format_profile_startup_banner(runner.db))
    print(f"  Unique companies : {summary['total']}")
    print(f"  Already scraped  : {summary['scraped']}")
    print(f"  Pending          : {summary['pending']}")
    print(f"  Partial          : {summary['partial']}")
    if summary.get("failed", 0):
        print(f"  Failed (retry)   : {summary['failed']}")
    if summary.get("not_found", 0):
        print(f"  Not found (404)  : {summary['not_found']}")
    print(f"  Workers          : {worker_count}")
    print(f"  Est. time        : ~{est_min:.0f} min")
    if keep_awake:
        print("  Keep-awake       : on (PC will not sleep during this run)")
    if interactive:
        answer = input("\nStart profile scraping? [Y/n]: ").strip().lower()
        if answer in ("n", "no"):
            raise SystemExit("Cancelled.")
    else:
        print("\nStarting profile scrape...")
    runner.run()


# ----------------------------------------------------------------------
# export / progress / maintenance
# ----------------------------------------------------------------------

EXPORT_TYPES = {
    "entity": "One row per company per entity (search terms merged)",
    "full": "Entity rows + profile details (website, phone, email, ...)",
    "profile": "Full company profiles (one row per company)",
    "websites": "Company name and website",
    "all": "All of the above",
}


def _pick_export_type_interactive() -> str:
    keys = list(EXPORT_TYPES)
    print("\nSelect export type:")
    for idx, key in enumerate(keys, start=1):
        print(f"  {idx}. {EXPORT_TYPES[key]}")
    while True:
        choice = input(f"\nEnter number [1-{len(keys)}]: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(keys):
            return keys[int(choice) - 1]
        print("Invalid choice.")


def cmd_export(args: argparse.Namespace) -> None:
    from src.storage.country_db import CountryDatabase
    from src.storage import exports

    continent, country, interactive = _resolve_target(args)
    export_type = args.type or (_pick_export_type_interactive() if interactive else "all")
    try:
        db = CountryDatabase(continent.code, country.name, read_only=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise SystemExit(f"\nExport failed: {exc}")
    fns = {
        "entity": [exports.export_entity_search_csv],
        "full": [exports.export_entity_full_csv],
        "profile": [exports.export_company_profiles_only_csv],
        "websites": [exports.export_companies_websites_csv],
    }
    fns["all"] = fns["entity"] + fns["full"] + fns["profile"] + fns["websites"]
    try:
        wrote = False
        for fn in fns[export_type]:
            path = fn(db)
            if path:
                wrote = True
                print(f"Exported: {path}")
        if not wrote:
            print("\nNo data to export for this type yet.")
    except PermissionError as exc:
        raise SystemExit(f"\nExport failed — is the CSV open in Excel? Close it and retry. ({exc})")
    finally:
        db.close()


def cmd_progress(args: argparse.Namespace) -> None:
    continent, country, _ = _resolve_target(args)
    print_progress(continent.code, country.name)


def cmd_profile_progress(args: argparse.Namespace) -> None:
    continent, country, _ = _resolve_target(args)
    print_profile_progress(continent.code, country.name)


def cmd_terms(args: argparse.Namespace) -> None:
    list_ids = [LIST_ALIASES[args.list]] if args.list else list(list_definitions())
    for list_id in list_ids:
        tl = load_term_list(list_id)
        print(
            f"\n=== {tl.label} — {len(tl.entities)} entities, {tl.term_count:,} terms "
            f"({tl.unique_term_count:,} unique) ===\nFile: {tl.path}"
        )
        _print_entities(tl)


def cmd_verify(args: argparse.Namespace) -> None:
    from src.storage.backup import list_backups, verify_database
    from src.storage.country_db import CountryDatabase
    from src.storage.progress_ledger import count_entries

    continent, country, _ = _resolve_target(args)
    db = CountryDatabase(continent.code, country.name, auto_recover=False)
    try:
        info = verify_database(db.conn, db.path)
        stats = db.search_stats()
        ledger = count_entries(continent.code, country.name)
        backups = list_backups(continent.code, country.name)
        print(f"\nVerify: {continent.code} -> {country.name}")
        print(f"  Path        : {info['path']}")
        print(f"  Size        : {info['size_mb']} MB")
        print(f"  Integrity   : {info['integrity']}")
        print(f"  Terms done  : {stats['terms_done']}")
        print(f"  Searches    : {stats['searches_done']} done, {stats.get('failed', 0)} failed")
        print(f"  Unique cos  : {db.unique_company_count()}")
        print(f"  Ledger      : {ledger} entries")
        print(f"  Backups     : {len(backups)} on disk")
        if backups:
            print(f"  Latest      : {backups[0]}")
    finally:
        db.close()


def cmd_backup(args: argparse.Namespace) -> None:
    from src.storage.backup import create_backup
    from src.storage.country_db import CountryDatabase

    continent, country, _ = _resolve_target(args)
    db = CountryDatabase(continent.code, country.name, auto_recover=False)
    try:
        path = create_backup(db.conn, continent.code, country.name)
        print(f"Backup saved: {path}")
    finally:
        db.close()


def cmd_verify_geo(args: argparse.Namespace) -> None:
    """Check every configured country spelling against live ensun (1 page each)."""
    from src.session import BrowserSession, run_async

    targets: list[tuple[Continent, Country]] = []
    for continent in load_continents():
        if args.continent and continent.code != args.continent.upper():
            continue
        for country in continent.countries:
            if args.country and country.name.lower() != args.country.lower():
                continue
            targets.append((continent, country))
    if not targets:
        raise SystemExit("No matching countries in config/geo_registry.yaml")

    browser = BrowserSession()

    async def _run() -> list[tuple[str, str, str, int | None, int, str]]:
        out = []
        await browser.start(headless=not args.show_browser)
        try:
            await browser.warm_up_cookies()
            for continent, country in targets:
                try:
                    companies, _pages, facets = await browser.fetch_search_pages(
                        args.term, continent.ensun_code, country.ensun_location, max_pages=1
                    )
                    seen = sorted({c.get("country") or "?" for c in companies})
                    out.append(
                        (
                            continent.code,
                            country.name,
                            country.ensun_location,
                            facets.total if facets else None,
                            len(companies),
                            ", ".join(seen[:4]),
                        )
                    )
                except Exception as exc:  # report and continue
                    out.append((continent.code, country.name, country.ensun_location, None, 0, f"ERROR {exc}"))
                print(f"  checked {country.name}: {out[-1][3]} / page1={out[-1][4]} [{out[-1][5]}]", flush=True)
        finally:
            await browser.close_search_session(force_cookie_save=True)
            await browser.stop()
        return out

    rows = run_async(_run())
    print(f"\nensun check for term {args.term!r} (count = ensun total; cards = countries on page 1):")
    for code, name, triple, total, n, seen in rows:
        flag = "OK " if total and n else "?? "
        print(f"  {flag}{code:<7} {name:<22} {triple:<34} count={total!s:<6} page1={n:<3} cards: {seen}")
    print(
        "\nOK  = ensun knows this location.  ?? = zero results: check the spelling in "
        "config/geo_registry.yaml (or the country simply has nothing for this term)."
    )


def cmd_capture(args: argparse.Namespace) -> None:
    from src.session import BrowserSession, run_async

    browser = BrowserSession()

    async def _run():
        await browser.start(headless=not args.show_browser)
        count = await browser.capture_graphql_baseline(args.url)
        await browser.stop()
        return count

    count = run_async(_run())
    print(f"GraphQL payloads saved: {count} file(s) under graphql/captured/")


# ----------------------------------------------------------------------

def _add_target(p: argparse.ArgumentParser) -> None:
    p.add_argument("--continent", help="ASIA, EUROPE or AFRICA (menu if omitted)")
    p.add_argument("--country", help="e.g. India (menu if omitted)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ensun agri search + profile scraper")
    sub = parser.add_subparsers(dest="command", required=True)

    search = sub.add_parser("search", help="Search phase for one country")
    _add_target(search)
    search.add_argument("--list", choices=["1", "2", "list1", "list2"], help="Search-term list")
    search.add_argument(
        "--entities",
        help='"all" (default), categories "1,3", entities "1.1,2.7", or mixed "1,2.7"',
    )
    search.add_argument("--terms-per-entity", type=int, help="Pilot: only the first N terms of each entity")
    search.add_argument("--max-terms", type=int, help="Pilot: stop after N terms finish")
    search.add_argument("--max-searches", type=int, help="Pilot: stop after N live ensun searches")
    search.add_argument("--force-rerun", action="store_true", help="Search again even if already done")
    search.add_argument("--show-browser", action="store_true")
    search.set_defaults(func=cmd_search)

    profile = sub.add_parser("profile", help="Scrape company profiles for found companies")
    _add_target(profile)
    profile.add_argument("--force-rerun", action="store_true")
    profile.add_argument("--limit", type=int, help="Max companies to scrape (pilot)")
    profile.add_argument("--workers", type=int, help="Parallel profile workers")
    profile.add_argument("--show-browser", action="store_true")
    profile.set_defaults(func=cmd_profile)

    export_cmd = sub.add_parser("export", help="Export CSVs for a country")
    _add_target(export_cmd)
    export_cmd.add_argument("--type", choices=list(EXPORT_TYPES), help="Export type (menu if omitted)")
    export_cmd.set_defaults(func=cmd_export)

    progress = sub.add_parser("progress", help="Search progress per entity for a country")
    _add_target(progress)
    progress.set_defaults(func=cmd_progress)

    profile_prog = sub.add_parser("profile-progress", help="Profile scrape progress")
    _add_target(profile_prog)
    profile_prog.set_defaults(func=cmd_profile_progress)

    terms = sub.add_parser("terms", help="Show categories/entities and term counts")
    terms.add_argument("--list", choices=["1", "2", "list1", "list2"])
    terms.set_defaults(func=cmd_terms)

    verify = sub.add_parser("verify", help="Integrity check and stats for a country DB")
    _add_target(verify)
    verify.set_defaults(func=cmd_verify)

    backup = sub.add_parser("backup", help="Manual backup of a country DB")
    _add_target(backup)
    backup.set_defaults(func=cmd_backup)

    geo = sub.add_parser("verify-geo", help="Check country spellings against live ensun")
    geo.add_argument("--continent")
    geo.add_argument("--country")
    geo.add_argument("--term", default="agriculture")
    geo.add_argument("--show-browser", action="store_true")
    geo.set_defaults(func=cmd_verify_geo)

    capture = sub.add_parser("capture", help="Capture GraphQL payloads from an ensun search page")
    capture.add_argument("--url", help="Optional search URL")
    capture.add_argument("--show-browser", action="store_true")
    capture.set_defaults(func=cmd_capture)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    from src.terms_loader import SearchTermsNotFoundError

    try:
        args.func(args)
    except SearchTermsNotFoundError as exc:
        # A setup problem, not a bug: show the instructions, not a traceback.
        raise SystemExit(f"\n{exc}") from None


if __name__ == "__main__":
    main()
