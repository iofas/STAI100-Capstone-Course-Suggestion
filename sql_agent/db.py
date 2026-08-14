"""
Database access helpers for the SQL Agent module.

Wraps the course_offerings SQLite database (built from
data/unformatted/*.txt via course_offerings_inserts.sql).
"""
import sqlite3
from contextlib import contextmanager

from .config import DB_PATH, SCHEDULE_TERM

TABLE_NAME = "course_offerings"
PREREQUISITES_TABLE_NAME = "course_prerequisites"


@contextmanager
def get_connection():
    """Yield a read-only SQLite connection to course_offerings.db.

    A context manager: the connection is opened with row access by column name
    (``sqlite3.Row``) and put into ``PRAGMA query_only`` mode so no statement
    run through it can modify the database, then closed on exit.

    Yields:
        sqlite3.Connection: A configured, read-only connection. Use it inside a
        ``with get_connection() as conn:`` block.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Defense-in-depth: reject any write at the SQLite engine level, so a bug
    # in the regex guardrail (agent.py) or a caller that bypasses it entirely
    # still can't modify the database through this connection.
    conn.execute("PRAGMA query_only = ON")
    try:
        yield conn
    finally:
        conn.close()


def _describe_table(conn, table_name: str) -> list[str]:
    """Introspect one table into human-readable schema lines.

    Args:
        conn: An open SQLite connection (from `get_connection`).
        table_name: The table to describe.

    Returns:
        Lines naming the table and each column (with type and a "(nullable)"
        marker), suitable for feeding to the LLM as schema context.

    Raises:
        RuntimeError: If the table does not exist in the database.
    """
    cur = conn.execute(f"PRAGMA table_info({table_name})")
    columns = cur.fetchall()
    if not columns:
        raise RuntimeError(
            f"Table '{table_name}' not found in {DB_PATH}. Did you load "
            f"course_offerings_inserts.sql into this database file?"
        )

    lines = [f"Table: {table_name}", "Columns:"]
    for col in columns:
        nullable = "" if col["notnull"] or col["pk"] else " (nullable)"
        lines.append(f"  - {col['name']} {col['type']}{nullable}")
    return lines


def get_schema_description() -> str:
    """
    Human-readable schema description fed to the LLM as grounding context.
    Introspects the live database rather than hardcoding column names, so the
    prompt always matches whatever is actually in course_offerings.db.
    """
    lines = []
    with get_connection() as conn:
        for table in [TABLE_NAME, PREREQUISITES_TABLE_NAME]:
            cur = conn.execute(f"PRAGMA table_info({table})")
            columns = cur.fetchall()
            
            lines.append(f"Table: {table}")
            lines.append("Columns:")
            for col in columns:
                nullable = "" if col["notnull"] or col["pk"] else " (nullable)"
                lines.append(f"  - {col['name']} {col['type']}{nullable}")
            lines.append("")

    lines.append(
        f"IMPORTANT - term: course_offerings holds multiple DLSU terms. Unless "
        f"the student explicitly asks about another term, ALWAYS restrict every "
        f"query to the current term with \"AND term = '{SCHEDULE_TERM}'\". Never "
        f"return rows from more than one term in the same answer. "
    )
    lines.append(
        "Notes: sched1_day / sched2_day use single-letter codes: "
        "M=Monday, T=Tuesday, W=Wednesday, H=Thursday, F=Friday, S=Saturday, "
        "U=Sunday. Note S is Saturday and U is Sunday - never use 'S' for a "
        "Sunday question. No classes are currently scheduled on Sunday, so a "
        "Sunday query correctly returns no rows. "
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
        "'GE', regardless of whether it starts with GE or LC - both "
        "prefixes belong to the same general-elective pool. "
        "When a student describes a free time window (e.g. 'a break between "
        "12:30 and 16:00'), a section only fits if ALL of its meetings are "
        "fully contained in that window - check sched1_time_start/"
        "sched1_time_end AND, when sched2_time_start is not NULL, also "
        "sched2_time_start/sched2_time_end, not just the first meeting. "
        "course_prerequisites records hard prerequisites: a row means "
        "course_code cannot be taken until prerequisite_code is completed. "
        "A course_code with no row in course_prerequisites has no "
        "prerequisite and is always eligible on that basis. When the "
        "student states which courses they have already completed, a "
        "course_code is only eligible to suggest if every one of its "
        "prerequisite_code rows is in that completed list - use NOT EXISTS "
        "against course_prerequisites to enforce this, not just NOT IN "
        "against the completed list on its own. "
        "teacher is stored as 'LASTNAME, FIRSTNAME MIDDLENAME' (e.g. "
        "'JIMENEZ, JOSE VICTOR DECENA'), but a student may type the name in "
        "any order. Never match on the full name as one substring in the "
        "student's word order - instead split the name into individual "
        "words and require each one to appear somewhere in teacher via a "
        "separate 'teacher LIKE %word%' ANDed together, so the match works "
        "regardless of what order the student typed the name in."
    )
    return "\n".join(lines)
