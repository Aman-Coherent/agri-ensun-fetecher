# Ensun Agri Scraper — notes for Claude

- Derived from `C:\Users\aman\Desktop\ensun - fecter` (nutraceutical). Never modify that project.
  Its test suite touches real country DBs (e.g. `verify` on finland.db) — don't run it casually.
- Search terms: `C:\Users\aman\Agro_Search_Terms` master CSVs (List 1 / List 2; 8 categories, 52 entities).
- Core flow: `src/search_phase.py` → `schedule.py` (round-robin) → `split_planner.py` (smart split,
  pure logic) → `session.fetch_search_pages` (returns companies + `facets` from GraphQL
  `getExpectedSearchResultCount`) → `storage/country_db.py` (`search_nodes`, `search_results`,
  `terms_done`, `term_entities`).
- Split logic v2 (user-approved, data-driven): two passes (`two_pass`), gap rule (`smart_split.min_gap`),
  industry first then company type (`smart_split.order`), early stop when all counted companies are found.
  Cost = page requests; never cut result pages (deep pages are the most novel).
- Industry split uses ONE global list `config/related_industries.yaml` (44 of ensun's 60), with
  per-industry counts from `session.count_results` (in-page GraphQL count query, stored in
  `search_nodes.industry_counts_json`). Not per entity — user decision.
- Node id = hash(location, term_key, company_type, industry) — no entity; shared terms searched once,
  credited to entities via `term_entities` at export (`storage/exports.py`).
- Profile phase (`profile_*.py`, `graphql/profile_*`) is copied verbatim from the old project.
- Tests: `python -m pytest -q tests` — `tests/conftest.py` redirects data/output/logs to a temp dir
  via `ENSUN_AGRI_RUNTIME_ROOT`; keep it that way.
- User-confirmed design decisions live in memory (`agri-scraper-design-decisions`).
