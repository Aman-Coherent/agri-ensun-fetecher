from __future__ import annotations

from src.geo import find_country
from src.storage.country_db import CountryDatabase


def _open_readonly(continent_code: str, country_name: str) -> tuple[CountryDatabase, str] | None:
    country = find_country(continent_code, country_name)
    if not country:
        print(f"Country not found: {country_name} in {continent_code}")
        return None
    try:
        return CountryDatabase(continent_code, country.name, read_only=True), country.ensun_location
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"\n{exc}")
        return None


def print_progress(continent_code: str, country_name: str) -> None:
    opened = _open_readonly(continent_code, country_name)
    if not opened:
        return
    db, location_triple = opened
    try:
        stats = db.search_stats()
        print(f"\nSearch progress: {continent_code} -> {db.country_name}")
        print(f"  Database          : {db.path}")
        print(f"  Terms fully done  : {stats['terms_done']:,}")
        print(f"  ...still at limit : {stats['capped_terms']:,} (ensun showed 100 max even after splitting)")
        print(f"  Searches done     : {stats['searches_done']:,}")
        print(f"  Skipped (same)    : {stats.get('same_as_parent', 0):,}")
        print(f"  Failed (retry)    : {stats.get('failed', 0):,}")
        print(f"  Pending           : {stats.get('pending', 0) + stats.get('in_progress', 0):,}")
        print(f"  Result rows       : {stats['result_rows']:,}")
        print(f"  Unique companies  : {db.unique_company_count():,}")

        rows = db.conn.execute(
            """
            SELECT te.list_id, te.entity_no, te.entity_name,
                   COUNT(*) AS total,
                   SUM(CASE WHEN td.term_key IS NOT NULL THEN 1 ELSE 0 END) AS done
            FROM term_entities te
            LEFT JOIN terms_done td
              ON td.term_key = te.term_key AND td.location_triple = ?
            GROUP BY te.list_id, te.entity_no, te.entity_name
            """,
            (location_triple,),
        ).fetchall()
        if not rows:
            return
        companies = {
            (r["list_id"], r["entity_no"]): r["cnt"]
            for r in db.conn.execute(
                """
                SELECT te.list_id, te.entity_no,
                       COUNT(DISTINCT COALESCE(NULLIF(sr.company_id, ''), sr.company_name)) AS cnt
                FROM search_results sr
                JOIN term_entities te ON te.term_key = sr.term_key
                GROUP BY te.list_id, te.entity_no
                """
            )
        }

        def sort_key(r) -> tuple:
            nums = tuple(int(p) if p.isdigit() else 999 for p in r["entity_no"].split("."))
            return (r["list_id"], nums)

        current_list = None
        for r in sorted(rows, key=sort_key):
            if r["list_id"] != current_list:
                current_list = r["list_id"]
                print(f"\n  {current_list}: terms done per entity")
            pct = (r["done"] / r["total"] * 100) if r["total"] else 0
            cos = companies.get((r["list_id"], r["entity_no"]), 0)
            print(
                f"    {r['entity_no']:>4} {r['entity_name'][:55]:<55} "
                f"{r['done']:>5}/{r['total']:<5} ({pct:3.0f}%)  {cos:,} companies"
            )
    finally:
        db.close()


def print_profile_progress(continent_code: str, country_name: str) -> None:
    opened = _open_readonly(continent_code, country_name)
    if not opened:
        return
    db, _ = opened
    try:
        stats = db.profile_stats()
        print(f"\nProfile progress: {continent_code} -> {db.country_name}")
        print(f"  Database: {db.path}")
        print(f"  Unique companies: {stats['total']}")
        print(f"  Profiles scraped: {stats['scraped']}")
        print(f"  Pending: {stats['pending']}")
        print(f"  Partial: {stats.get('partial', 0)}")
        print(f"  Failed: {stats.get('failed', 0)}")

        sample = db.conn.execute(
            """
            SELECT company_name, scrape_status, main_city, main_country, employee_range, working_industry
            FROM company_profiles
            ORDER BY scraped_at DESC
            LIMIT 5
            """
        ).fetchall()
        if sample:
            print("\n  Recent profiles:")
            for row in sample:
                print(
                    f"    {row['company_name']} | {row['scrape_status']} | "
                    f"{row['main_city']}, {row['main_country']} | {row['employee_range']} | {row['working_industry']}"
                )
    finally:
        db.close()
