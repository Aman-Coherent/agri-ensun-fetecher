"""Append-only audit ledger of finished searches / profiles (one JSON per line).

Each finished unit of work is fsync'd to ``data/{continent}/{country}.progress.jsonl``
so there is a durable record independent of the SQLite file.

Unlike the old nutraceutical scraper, the ledger is *not* used to mark work as
done after a database loss: that restored the "done" flag without the result
rows, silently hiding missing data. Lost databases are restored from backups
(see storage/backup.py); anything still missing is simply re-searched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from src.config_loader import DATA_DIR, country_slug, ensure_dirs


def ledger_path(continent_code: str, country_name: str) -> Path:
    slug = country_slug(country_name)
    ensure_dirs(DATA_DIR / continent_code)
    return DATA_DIR / continent_code / f"{slug}.progress.jsonl"


def append_entry(continent_code: str, country_name: str, entry: dict[str, Any]) -> None:
    path = ledger_path(continent_code, country_name)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def load_entries(continent_code: str, country_name: str) -> list[dict[str, Any]]:
    path = ledger_path(continent_code, country_name)
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                # A hard power-off can leave a half-written last line.
                continue
    return entries


def count_entries(continent_code: str, country_name: str) -> int:
    path = ledger_path(continent_code, country_name)
    if not path.exists():
        return 0
    with path.open(encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())
