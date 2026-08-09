"""Load course_prerequisites from scraper/prerequisites.txt.

Each line is "COURSE <- PREREQ" (COURSE needs PREREQ first). By default new
pairs are added; --replace wipes the table first and loads exactly this file.

    python -m scraper.load_prerequisites                 # add to existing
    python -m scraper.load_prerequisites --replace       # authoritative reload

Course codes are checked against course_offerings and any that never appear as
an offering are flagged (likely a typo), but still loaded.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path

PAIR_RE = re.compile(r"^\s*([A-Za-z0-9\-]+)\s*<-\s*([A-Za-z0-9\-]+)\s*$")


def read_pairs(path: Path) -> list[tuple[str, str]]:
    """Parse a prerequisites file into (course, prerequisite) pairs.

    Args:
        path: Path to a text file with one ``COURSE <- PREREQ`` rule per line.
            Blank lines and lines starting with ``#`` are ignored.

    Returns:
        A list of ``(course_code, prerequisite_code)`` tuples, both upper-cased.

    Raises:
        SystemExit: On the first line that does not match the expected
            ``COURSE <- PREREQ`` format (message includes the line number).
    """
    pairs: list[tuple[str, str]] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = PAIR_RE.match(s)
        if not m:
            raise SystemExit(f"{path}:{n}: expected 'COURSE <- PREREQ', got: {line!r}")
        pairs.append((m.group(1).upper(), m.group(2).upper()))
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", type=Path, default=Path("scraper/prerequisites.txt"))
    ap.add_argument("--db", type=Path, default=Path("course_offerings.db"))
    ap.add_argument("--replace", action="store_true",
                    help="delete all existing prerequisites before loading")
    args = ap.parse_args()

    pairs = read_pairs(args.file)
    if not pairs:
        print("No prerequisite pairs found.")
        return 0

    conn = sqlite3.connect(args.db)
    try:
        known = {r[0] for r in conn.execute("SELECT DISTINCT course_code FROM course_offerings")}
        unknown = sorted({c for pair in pairs for c in pair if c not in known})
        if unknown:
            print(f"! Warning: these codes never appear in course_offerings "
                  f"(typo?): {unknown}")

        if args.replace:
            deleted = conn.execute("DELETE FROM course_prerequisites").rowcount
            print(f"Cleared {deleted} existing prerequisite row(s).")

        before = conn.execute("SELECT COUNT(*) FROM course_prerequisites").fetchone()[0]
        conn.executemany(
            "INSERT OR IGNORE INTO course_prerequisites "
            "(course_code, prerequisite_code) VALUES (?, ?)",
            pairs,
        )
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM course_prerequisites").fetchone()[0]
        print(f"Loaded {len(pairs)} pair(s); table went {before} -> {after} rows.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
