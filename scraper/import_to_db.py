"""Load scraped offerings (CSV or JSON) into course_offerings.db.

Run this only after eyeballing the scraped file. By default it appends;
pass --replace-codes to first delete existing rows for the scraped course
codes (a clean re-import for those courses).

    python -m scraper.import_to_db --in scraper/out/ge_offerings.json
    python -m scraper.import_to_db --in scraper/out/ge_offerings.csv --replace-codes
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from pathlib import Path

COLUMNS = [
    "term", "course_code", "teacher", "section",
    "sched1_day", "sched1_time_start", "sched1_time_end",
    "sched2_day", "sched2_time_start", "sched2_time_end",
    "room", "remarks",
]


def load_rows(path: Path) -> list[dict]:
    """Load scraped offering rows from a ``.json`` or ``.csv`` file.

    Args:
        path: Path to the input file. A ``.json`` suffix is parsed as JSON;
            anything else is read as CSV with a header row.

    Returns:
        The rows as a list of dicts (column name -> value).
    """
    if path.suffix.lower() == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def norm(v):
    """Normalise an empty cell to SQL NULL.

    Args:
        v: A raw cell value (typically a string from CSV/JSON).

    Returns:
        None if `v` is an empty string or None, otherwise `v` unchanged - so
        blank cells land in the DB as NULL rather than ``''``.
    """
    return None if v in ("", None) else v


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="infile", type=Path, required=True)
    ap.add_argument("--db", type=Path, default=Path("course_offerings.db"))
    ap.add_argument("--replace-codes", action="store_true",
                    help="delete existing rows for the scraped course codes first")
    args = ap.parse_args()

    rows = load_rows(args.infile)
    if not rows:
        print("No rows to import.")
        return 0

    conn = sqlite3.connect(args.db)
    try:
        if args.replace_codes:
            # Scope deletes by (term, course_code) so re-importing one term
            # never touches rows from another term with the same course code.
            pairs = sorted({(r["term"], r["course_code"]) for r in rows})
            deleted = 0
            for term, code in pairs:
                deleted += conn.execute(
                    "DELETE FROM course_offerings WHERE term=? AND course_code=?",
                    (term, code),
                ).rowcount
            terms = sorted({t for t, _ in pairs})
            print(f"Deleted {deleted} existing row(s) for term(s) {terms}.")

        placeholders = ",".join("?" * len(COLUMNS))
        conn.executemany(
            f"INSERT INTO course_offerings ({','.join(COLUMNS)}) VALUES ({placeholders})",
            [tuple(norm(r.get(c)) for c in COLUMNS) for r in rows],
        )
        conn.commit()
        print(f"Inserted {len(rows)} row(s) into {args.db}.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
