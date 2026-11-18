# Ensun Agri Scraper

Finds agriculture companies on [ensun.io](https://ensun.io) for each country, using the
agri search-term lists in `C:\Users\aman\Agro_Search_Terms`, then (optionally) opens each
company's profile for website / phone / email.

Built from the nutraceutical scraper (`Desktop\ensun - fecter`), reusing its proven
browser, block/VPN recovery, crash-safe database and profile code. The old project is
not modified.

## Setup (once)

```powershell
cd "C:\Users\aman\Desktop\ensun-agri-fetecher"
pip install -r requirements.txt
playwright install chromium
```

## Everyday use

### 1. Search (Step 1)

```powershell
python -m src.cli search
```

The menu asks, in order:

1. **Continent** — ASIA, EUROPE or AFRICA
2. **Country** — shown with its project phase, e.g. `India (Phase 1)`
3. **List** — `List 1 - Entire Coverage` (14,065 terms) or `List 2 - Top Search Terms` (3,949 terms)
4. **Entities** — press Enter for all 52, or type:
   - `1,3` → whole categories 1 and 3
   - `1.1,2.7` → just those two entities
   - `1,2.7` → all of category 1 plus entity 2.7

Then it shows a summary and asks `Start scraping? [Y/n]`.

Same thing without menus (good for a second terminal):

```powershell
python -m src.cli search --continent ASIA --country India --list 2
python -m src.cli search --continent ASIA --country India --list 2 --entities 1.1,1.3
```

Pilot / test options: `--terms-per-entity 2`, `--max-terms 10`, `--max-searches 20`,
`--show-browser` (watch the browser).

### 2. Profiles (Step 2)

```powershell
python -m src.cli profile
```

Opens every company found in Step 1 and saves website, phone, email, LinkedIn,
address, employees, etc. You can run this any time (also while Step 1 is still going).

### 3. Export CSVs

```powershell
python -m src.cli export
```

Files go to `output\{CONTINENT}\{country}\`:

| File | One row per | What's in it |
|---|---|---|
| `{country}_search_by_entity.csv` | company **per entity** | entity, company, all search terms that found it (merged with ` \| `), company type / industry filters, grade, city, key takeaway |
| `{country}_entity_full.csv` | company **per entity** | everything above **plus** profile data (website, phone, email, LinkedIn, ...) |
| `{country}_company_profiles.csv` | company | full profile sheet |
| `{country}_companies_websites.csv` | company | name + website |

The search CSV is also refreshed automatically every 100 searches and at the end of a run.
Files open correctly in Excel (accents like "Côte d'Ivoire" are preserved).

### Other commands

```powershell
python -m src.cli progress            # terms done per entity, companies per entity
python -m src.cli profile-progress
python -m src.cli terms --list 2      # show categories / entity numbers / term counts
python -m src.cli verify              # database health check
python -m src.cli backup              # manual backup
python -m src.cli verify-geo          # check country spellings against live ensun
```

## How a search term is searched ("smart split")

A free ensun account shows **at most 100 companies per search**, and every page of 10
results costs one request (1-8 s, depending on how fast ensun answers). So the scraper
spends requests only where companies are actually **missing**: the *gap* = ensun's count
minus the companies already found.

**Pass 1 — every term, no filter** (round-robin across entities):
- gap under 20 (e.g. count 87, or count 110 with 100 found) → **done in 1 search**.
- gap 20 or more → the term is queued for Pass 2.

**Pass 2 — filters for the queued terms, biggest gap first:**
1. One quick **count check** (no page loads) for the **44 agri-related industries**
   (`config/related_industries.yaml`; the 16 unrelated ones like Gaming or Real Estate are
   never used).
2. Search each industry that has companies, **largest first** — including industries
   ensun's filter panel doesn't show in its top 5.
3. An industry that itself still has a gap of 20+ is split by **company type**
   (Manufacturer, Service provider, Distributor, ...).
4. **Stop as soon as every company ensun counted has been found** — remaining smaller
   searches are skipped.

Why this order (measured on the UAE and India runs, 2026-11-11):
- Most terms need no filter at all (UAE: 23 of 24 terms had ≤ 100 companies).
- Splitting a group of 102 where 100 were already found cost 20 searches for 2 companies
  → the gap rule.
- The "Manufacturer" group is often ~80% of all results, so searching it mostly repeats
  the first search (India: 12 new of 83); industry groups are smaller and overlap little
  (~86% new) → industry first.
- Deep result pages hold the most new companies (page 1: 30% new, page 10: 77%), so
  pages are never cut short.

Example, "pesticide" in India: 1,621 companies → Pass 2 → Chemicals 606, Consumer Goods
159, Pharma 153, Agriculture 126, Biotech 64, Plastics, Other, Wholesale, ... → Chemicals
(still 500+ missing) split by company type.

A filter that narrows nothing is skipped without a search. If a search stops paging early
(ensun throttling), it is retried until it has as many companies as ensun's count says (up
to 3 tries; best attempt kept). Settings: `two_pass` and `smart_split` (`order`,
`min_gap`, ...) in `config/settings.yaml`; `order: type_first` restores the earlier
company-type-first method.

## Order of work and shared terms

- **Round-robin** (default): 1st term of every chosen entity, then the 2nd term of every
  entity, ... — if you stop early, every entity has some data.
  (`search_schedule: sequential` in settings = finish one entity before the next.)
- A term listed under several entities (e.g. "fungicide" in 1.1, 1.3, 1.5) is **searched
  once** per country; its companies are credited to every entity that lists it.
- **Same company, same entity, several terms → one row**, terms merged.
  Same company in two different entities → one row in each entity.

## Stop / resume / safety

- **Ctrl+C** any time. Run the same command again — finished terms are skipped,
  half-finished terms continue where they stopped (finished searches are reused).
- A search that failed (after all retries / VPN rotation) is retried on the next run.
- Each finished search is saved in one step (companies + status together), so a crash
  never leaves a search marked done without its companies.
- Rolling backups in `data\backups\`, append-only log in `data\{CONTINENT}\{country}.progress.jsonl`.
- Don't run the **same country** in two terminals (the scraper refuses). Different countries
  in parallel is fine — each has its own database.
- Interactive runs keep the PC awake.

## Auto VPN rotate on blocks

Same as the old scraper: start the Proton VPN automation tool first
(`Desktop\automation tool\Open App.bat`), keep Proton VPN connected. On a block the scraper
asks it for a new server, refreshes cookies and continues. If the tool isn't running, it
just retries normally.

One difference: ensun's **429 browser check** (shown on a new IP or when cookies expire)
usually passes by itself within a few seconds. The scraper now waits up to 15 seconds for it
(`browser_check_grace_seconds`), reloads, and carries on **without** changing the VPN server.
Only if the check doesn't pass — or on real blocks (403 Forbidden, connection drops) — does it
rotate the VPN as before. Set `browser_check_grace_seconds: 0` for the old behaviour.

## Company IDs

Every company card gets ensun's company ID — needed for the profile step. Most cards carry it
in their logo address; cards with ensun's default picture (~20%) don't, so the ID is read from
the company record behind the card instead (matched by exact name). Before this fix those
companies had no ID and never got a profile.

## Data layout

```
data\sessions.db                     ensun cookies (no login needed)
data\{CONTINENT}\{country}.db        everything for that country
data\backups\{CONTINENT}\{country}\  rolling backups
output\{CONTINENT}\{country}\        CSV exports
logs\{CONTINENT}\{country}_progress.log
```

## Search terms

Read from the master files (one per list):

- `Agro_Search_Terms\List 1 - Entire Coverage\0. ALL ENTITIES - List 1 (Master).csv`
- `Agro_Search_Terms\List 2 - Top Search Terms\0. ALL ENTITIES - List 2 (Master).csv`

If you edit terms, edit the **master** file (the per-entity files are not read).
Folder location: `search_terms_root` in `config/settings.yaml`.

## Tests

```powershell
python -m pytest -q tests
```

Tests run in a temporary folder and never touch real data.
