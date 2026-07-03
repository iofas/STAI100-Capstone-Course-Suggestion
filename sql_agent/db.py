"""
Database access helpers for the SQL Agent module.

Wraps the course_offerings SQLite database (built from
data/unformatted/*.txt via course_offerings_inserts.sql).
"""
import sqlite3
from contextlib import contextmanager

from .config import DB_PATH

TABLE_NAME = "course_offerings"


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def get_schema_description() -> str:
    """
    Human-readable schema description fed to the LLM as grounding context.
    Introspects the live database rather than hardcoding column names, so the
    prompt always matches whatever is actually in course_offerings.db.
    """
    with get_connection() as conn:
        cur = conn.execute(f"PRAGMA table_info({TABLE_NAME})")
        columns = cur.fetchall()

    if not columns:
        raise RuntimeError(
            f"Table '{TABLE_NAME}' not found in {DB_PATH}. Did you load "
            f"course_offerings_inserts.sql into this database file?"
        )

    lines = [f"Table: {TABLE_NAME}", "Columns:"]
    for col in columns:
        nullable = "" if col["notnull"] or col["pk"] else " (nullable)"
        lines.append(f"  - {col['name']} {col['type']}{nullable}")

    lines.append("")
    lines.append(
        "Notes: sched1_day / sched2_day use single-letter codes: "
        "M=Monday, T=Tuesday, W=Wednesday, H=Thursday, F=Friday, S=Saturday. "
        "Most sections meet twice a week (sched1_* + sched2_*); some meet "
        "only once, in which case sched2_* columns are NULL. Times are "
        "stored as 'HH:MM' 24-hour strings, so comparisons like "
        "sched1_time_start < '13:00' work as plain string comparisons. "
        "course_code identifies the subject; section identifies a specific "
        "offering of that subject. Even for 'suggest subjects' questions, "
        "still return one row per matching section (course_code, section, "
        "schedule, teacher) rather than collapsing to DISTINCT course_code, "
        "unless the student explicitly asks to exclude one of those fields. "
        "Students colloquially call every course_code in this table a "
        "'GE subject', regardless of whether it starts with GE or LC - both "
        "prefixes belong to the same general-elective pool. "
        "When a student describes a free time window (e.g. 'a break between "
        "12:30 and 16:00'), a section only fits if ALL of its meetings are "
        "fully contained in that window - check sched1_time_start/"
        "sched1_time_end AND, when sched2_time_start is not NULL, also "
        "sched2_time_start/sched2_time_end, not just the first meeting."
    )
    return "\n".join(lines)
