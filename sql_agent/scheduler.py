"""
Deterministic schedule solver (the "data science" layer).

This module is the anti-hallucination core of the project. The LLM only ever
translates a student's natural-language request into a structured
``ScheduleConstraints`` object (see schedule_agent.py). The actual timetable is
built here by an Integer Linear Program solved with Google OR-Tools' CP-SAT
engine - a deterministic optimizer that CANNOT invent a section or return a
schedule with a time clash, because every constraint is checked explicitly.

Design (see docs/RRL.md):
  * Hard constraints  -> filter the candidate pool + model constraints
                         (one section per course, no time overlap, max
                         classes/day, allowed on-campus days, time window).
  * Soft preference   -> objective term (minimise time spent on campus / gaps).
  * Infeasibility     -> a relaxation loop that loosens the lowest-priority
                         hard constraint, re-solves, and records what it did,
                         so the agent can explain the trade-off instead of
                         failing or hallucinating (Fix #2 / the negotiation
                         loop).

The core ``solve_with_relaxation`` takes plain Section objects and has no LLM or
DB dependency, so it is fully unit-testable offline (see test_scheduler.py).
``ortools`` is imported lazily so ``import sql_agent`` still works for the
SQL-only lookup path even if OR-Tools isn't installed.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from . import config
from .db import get_connection

DAY_ORDER = ["M", "T", "W", "H", "F", "S"]
DAY_NAMES = {
    "M": "Monday", "T": "Tuesday", "W": "Wednesday",
    "H": "Thursday", "F": "Friday", "S": "Saturday",
}
_DAY_MIN = 0
_DAY_MAX = 24 * 60  # minutes in a day, used as the IntVar upper bound


def to_minutes(hhmm: Optional[str]) -> Optional[int]:
    """Convert a 24-hour clock string to minutes since midnight.

    Args:
        hhmm: A time as ``'HH:MM'`` or ``'HH:MM:SS'`` (e.g. ``'13:30'``).
            None or an empty string is treated as "no bound".

    Returns:
        Minutes since midnight (``13:30`` -> ``810``), or None if `hhmm` was
        None/empty. Seconds, if present, are ignored.
    """
    if not hhmm:
        return None
    parts = str(hhmm).strip().split(":")
    return int(parts[0]) * 60 + int(parts[1])


def minutes_to_hhmm(m: Optional[int]) -> Optional[str]:
    """Inverse of `to_minutes`: minutes since midnight -> ``'HH:MM'`` string.

    Args:
        m: Minutes since midnight (e.g. ``810``), or None.

    Returns:
        A zero-padded ``'HH:MM'`` string (``810`` -> ``'13:30'``), or None if
        `m` was None.
    """
    if m is None:
        return None
    return f"{m // 60:02d}:{m % 60:02d}"


@dataclass(frozen=True)
class Meeting:
    """One weekly meeting: a day and a [start, end) minute interval."""
    day: str
    start: int
    end: int


@dataclass
class Section:
    """One offering of a course - the schedulable unit."""
    course_code: str
    section: str
    teacher: Optional[str] = None
    room: Optional[str] = None
    remarks: Optional[str] = None
    meetings: list[Meeting] = field(default_factory=list)
    raw: dict = field(default_factory=dict)  # original DB row, echoed to output

    @property
    def days(self) -> set[str]:
        return {m.day for m in self.meetings}


def sections_conflict(a: Section, b: Section) -> bool:
    """Test whether two sections have a time clash.

    Two sections conflict if any meeting of one overlaps any meeting of the
    other on the same weekday (half-open intervals, so back-to-back classes
    that merely touch at an endpoint do NOT count as a clash).

    Args:
        a: One section.
        b: The other section.

    Returns:
        True if the two sections cannot both be attended; False otherwise.
    """
    for ma in a.meetings:
        for mb in b.meetings:
            if ma.day == mb.day and ma.start < mb.end and mb.start < ma.end:
                return True
    return False


@dataclass
class ScheduleConstraints:
    """Structured constraints extracted from the student's request (Step 1)."""
    desired_courses: list[str] = field(default_factory=list)  # explicit codes
    desired_count: Optional[int] = None      # "give me N GE subjects" mode
    completed_courses: list[str] = field(default_factory=list)
    earliest: Optional[str] = None           # 'HH:MM' - no class starts before
    latest: Optional[str] = None             # 'HH:MM' - no class ends after
    allowed_days: Optional[list[str]] = None  # restrict on-campus days
    max_per_day: Optional[int] = None
    no_gaps: bool = False                    # minimise time spent on campus


@dataclass
class ScheduleResult:
    feasible: bool
    chosen: list[Section] = field(default_factory=list)
    dropped_courses: list[str] = field(default_factory=list)
    relaxations: list[str] = field(default_factory=list)
    campus_minutes: Optional[int] = None     # total per-day span, if optimised
    # The constraints actually in force for the returned schedule (== the
    # original request, unless the negotiation loop loosened some). Correctness
    # is judged against THESE; the relaxations list documents the deviation.
    effective: Optional["ScheduleConstraints"] = None


# --------------------------------------------------------------------------- #
# Hard-constraint filtering
# --------------------------------------------------------------------------- #
def _passes_time_day(section: Section, earliest: Optional[int],
                     latest: Optional[int], allowed_days: Optional[set[str]]) -> bool:
    """Check a section against the time-window and allowed-day hard filters.

    A section is only usable if EVERY one of its meetings fits the window and
    falls on an allowed day - the student has to attend all of them.

    Args:
        section: The section to test.
        earliest: Earliest allowed start, in minutes since midnight; no lower
            bound if None.
        latest: Latest allowed end, in minutes since midnight; no upper bound
            if None.
        allowed_days: Set of permitted day codes (subset of DAY_ORDER, e.g.
            ``{"M", "W"}``); no day restriction if None.

    Returns:
        True if all of the section's meetings satisfy the given bounds.
    """
    for m in section.meetings:
        if allowed_days is not None and m.day not in allowed_days:
            return False
        if earliest is not None and m.start < earliest:
            return False
        if latest is not None and m.end > latest:
            return False
    return True


def _filter_pool(sections: list[Section], earliest: Optional[str],
                 latest: Optional[str], allowed_days: Optional[list[str]]) -> list[Section]:
    """Narrow the candidate pool to sections that pass the time/day filters.

    Thin wrapper over `_passes_time_day` that accepts the human-facing
    ``'HH:MM'`` / day-list forms and converts them once for the whole pool.

    Args:
        sections: All candidate sections to filter.
        earliest: Earliest allowed start as ``'HH:MM'`` (or None for no bound).
        latest: Latest allowed end as ``'HH:MM'`` (or None for no bound).
        allowed_days: List of permitted day codes (or None for no restriction).

    Returns:
        The subset of `sections` whose meetings all fit the window and days.
    """
    e, l = to_minutes(earliest), to_minutes(latest)
    days = set(allowed_days) if allowed_days else None
    return [s for s in sections if _passes_time_day(s, e, l, days)]


# --------------------------------------------------------------------------- #
# The ILP itself (CP-SAT)
# --------------------------------------------------------------------------- #
def _solve_ilp(pool: list[Section], count_limit: Optional[int],
               max_per_day: Optional[int], no_gaps: bool) -> list[Section]:
    """
    Choose at most one section per course such that no two chosen sections
    clash, respecting max_per_day and (in count mode) a cap on how many
    courses to pick. Objective: primarily MAXIMISE the number of courses
    scheduled; secondarily (when no_gaps) MINIMISE total time on campus.

    Args:
        pool: The candidate sections to choose from (already time/day
            filtered). One boolean decision variable is created per section.
        count_limit: In "give me N subjects" mode, the maximum number of
            distinct courses to include; None means include as many as
            possible (used in explicit-course-list mode).
        max_per_day: Maximum number of classes allowed on any single day, or
            None for no per-day cap.
        no_gaps: If True, add a secondary objective that minimises total
            per-day campus span (packs classes, reduces idle gaps); course
            inclusion still strictly dominates compactness.

    Returns:
        The chosen sections (possibly fewer than requested if the request is
        over-constrained), or ``[]`` if the pool is empty or the model is
        infeasible.
    """
    if not pool:
        return []

    from ortools.sat.python import cp_model  # lazy import

    model = cp_model.CpModel()
    x = {i: model.NewBoolVar(f"x_{i}") for i in range(len(pool))}

    by_course: dict[str, list[int]] = defaultdict(list)
    for i, s in enumerate(pool):
        by_course[s.course_code].append(i)

    # At most one section per course; inc[c] = 1 iff course c is scheduled.
    inc = {}
    for c, idxs in by_course.items():
        model.Add(sum(x[i] for i in idxs) <= 1)
        inc[c] = model.NewBoolVar(f"inc_{c}")
        model.Add(inc[c] == sum(x[i] for i in idxs))

    # No two chosen sections may clash in time.
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            if pool[i].course_code != pool[j].course_code and sections_conflict(pool[i], pool[j]):
                model.Add(x[i] + x[j] <= 1)

    # Cap on classes per day.
    if max_per_day is not None:
        for d in DAY_ORDER:
            day_idxs = [i for i, s in enumerate(pool) if d in s.days]
            if day_idxs:
                model.Add(sum(x[i] for i in day_idxs) <= max_per_day)

    # Count mode: pick at most N courses total.
    if count_limit is not None:
        model.Add(sum(inc.values()) <= count_limit)

    included = sum(inc.values())

    span_terms = []
    if no_gaps:
        # Per-day campus span = (latest end) - (earliest start) among chosen
        # sections meeting that day; 0 if the day is free. Minimising the sum
        # packs classes together and cuts idle gaps.
        for d in DAY_ORDER:
            day_meetings = [(i, m) for i, s in enumerate(pool)
                            for m in s.meetings if m.day == d]
            if not day_meetings:
                continue
            start_d = model.NewIntVar(_DAY_MIN, _DAY_MAX, f"start_{d}")
            end_d = model.NewIntVar(_DAY_MIN, _DAY_MAX, f"end_{d}")
            span_d = model.NewIntVar(0, _DAY_MAX, f"span_{d}")
            for i, m in day_meetings:
                model.Add(end_d >= m.end).OnlyEnforceIf(x[i])
                model.Add(start_d <= m.start).OnlyEnforceIf(x[i])
            model.Add(span_d >= end_d - start_d)
            span_terms.append(span_d)

    if span_terms:
        # BIG makes course inclusion strictly dominate compactness: never drop
        # a course just to save minutes (max possible span sum < BIG).
        BIG = _DAY_MAX * len(DAY_ORDER) + 1
        model.Maximize(BIG * included - sum(span_terms))
    else:
        model.Maximize(included)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10.0
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return []
    return [pool[i] for i in range(len(pool)) if solver.Value(x[i]) == 1]


def campus_span_minutes(chosen: list[Section]) -> int:
    """Sum the per-day time-on-campus of a schedule (the compactness metric).

    For each weekday, the span is (last meeting end - first meeting start);
    free days contribute 0. Lower is more compact.

    Args:
        chosen: The sections making up the schedule.

    Returns:
        Total minutes between first start and last end, summed over all days.
    """
    total = 0
    for d in DAY_ORDER:
        starts = [m.start for s in chosen for m in s.meetings if m.day == d]
        ends = [m.end for s in chosen for m in s.meetings if m.day == d]
        if starts:
            total += max(ends) - min(starts)
    return total


# --------------------------------------------------------------------------- #
# The negotiation loop (Fix #2)
# --------------------------------------------------------------------------- #
def solve_with_relaxation(sections: list[Section],
                          c: ScheduleConstraints) -> ScheduleResult:
    """
    Try to satisfy every requested course under the given constraints. If that
    is infeasible, loosen the lowest-priority hard constraint, re-solve, and
    record the relaxation - so the caller can explain the trade-off. Order of
    relaxation (least painful first):
        1. raise max classes/day
        2. drop the on-campus-day restriction
        3. widen the class-time window
        4. accept dropping course(s)  [reported, never silent]

    This is the module's main entry point and has no LLM/DB dependency, so it
    is fully unit-testable offline.

    Args:
        sections: The candidate pool (typically from `fetch_eligible_sections`,
            but any list of Section objects works - that is what makes it
            testable with hand-built data).
        c: The structured constraints extracted from the student's request
            (desired courses or a target count, time window, allowed days,
            max-per-day, compactness preference).

    Returns:
        A `ScheduleResult` recording:
            - ``feasible``/``chosen``: the selected sections (empty if nothing
              could be scheduled);
            - ``dropped_courses``: requested courses that could not be placed;
            - ``relaxations``: human-readable notes on every constraint that
              was loosened, in the order it happened;
            - ``campus_minutes``: compactness of the result, if any;
            - ``effective``: the constraints actually in force for the returned
              schedule (correctness is judged against these).
    """
    if c.desired_count:
        target: Optional[set[str]] = None          # count mode: no fixed set
    else:
        target = {code.upper() for code in c.desired_courses}

    cur_e, cur_l = c.earliest, c.latest
    cur_days = list(c.allowed_days) if c.allowed_days else None
    cur_max = c.max_per_day
    relax: list[str] = []

    def attempt() -> list[Section]:
        pool = _filter_pool(sections, cur_e, cur_l, cur_days)
        return _solve_ilp(pool, c.desired_count, cur_max, c.no_gaps)

    def is_complete(chosen: list[Section]) -> bool:
        got = {s.course_code.upper() for s in chosen}
        if c.desired_count:
            return len(got) >= c.desired_count
        return target.issubset(got) if target else True

    chosen = attempt()

    # Upper bound for the max-per-day relaxation: never need more slots/day
    # than the number of courses we're trying to place.
    max_needed = c.desired_count or (len(target) if target else 0)

    while not is_complete(chosen):
        if cur_max is not None and cur_max < max_needed:
            cur_max += 1
            relax.append(f"allowed up to {cur_max} classes on a single day")
            chosen = attempt()
            continue
        if cur_days is not None and set(cur_days) != set(DAY_ORDER):
            cur_days = None
            relax.append("lifted the on-campus-days restriction")
            chosen = attempt()
            continue
        if cur_e is not None or cur_l is not None:
            cur_e = cur_l = None
            relax.append("widened the earliest/latest class-time window")
            chosen = attempt()
            continue
        break  # nothing left to relax -> report dropped course(s)

    got = {s.course_code.upper() for s in chosen}
    dropped = sorted(target - got) if target else []
    effective = ScheduleConstraints(
        desired_courses=list(c.desired_courses),
        desired_count=c.desired_count,
        completed_courses=list(c.completed_courses),
        earliest=cur_e,
        latest=cur_l,
        allowed_days=cur_days,
        max_per_day=cur_max,
        no_gaps=c.no_gaps,
    )
    return ScheduleResult(
        feasible=bool(chosen),
        chosen=chosen,
        dropped_courses=dropped,
        relaxations=relax,
        campus_minutes=campus_span_minutes(chosen) if chosen else None,
        effective=effective,
    )


# --------------------------------------------------------------------------- #
# Candidate retrieval (deterministic SQL, not LLM-generated)
# --------------------------------------------------------------------------- #
_SCHEDULE_COLUMNS = (
    "course_code, section, teacher, sched1_day, sched1_time_start, "
    "sched1_time_end, sched2_day, sched2_time_start, sched2_time_end, "
    "room, remarks"
)


def _row_to_section(row: dict) -> Section:
    """Convert one course_offerings DB row into a `Section`.

    Flattens the table's two schedule slots (``sched1_*`` / ``sched2_*``) into
    a single list of `Meeting` intervals, skipping any slot whose day or times
    are missing (e.g. fully-online sections).

    Args:
        row: A mapping of column name -> value for one section, as returned by
            the retrieval query (see `_SCHEDULE_COLUMNS`).

    Returns:
        A `Section` with its meetings parsed to minute intervals; the original
        row is preserved in ``Section.raw`` so it can be echoed to the output.
    """
    meetings = []
    if row.get("sched1_day"):
        s, e = to_minutes(row["sched1_time_start"]), to_minutes(row["sched1_time_end"])
        if s is not None and e is not None:
            meetings.append(Meeting(row["sched1_day"], s, e))
    if row.get("sched2_day"):
        s, e = to_minutes(row["sched2_time_start"]), to_minutes(row["sched2_time_end"])
        if s is not None and e is not None:
            meetings.append(Meeting(row["sched2_day"], s, e))
    return Section(
        course_code=row["course_code"],
        section=row["section"],
        teacher=row.get("teacher"),
        room=row.get("room"),
        remarks=row.get("remarks"),
        meetings=meetings,
        raw=dict(row),
    )


def fetch_eligible_sections(desired_courses: Optional[list[str]],
                            completed_courses: Optional[list[str]],
                            term: Optional[str] = None) -> list[Section]:
    """
    Pull candidate sections from course_offerings using a parameterised,
    read-only query built in Python (never LLM-generated). Applies the same
    two hard eligibility rules the SQL agent uses:
      * exclude courses the student has already completed, and
      * exclude any course with an unmet prerequisite (NOT EXISTS against
        course_prerequisites).
    Time/day/gap constraints are applied later, in the solver, so the
    relaxation loop can loosen them without re-querying.

    Args:
        desired_courses: Course codes the student explicitly asked for; if
            None/empty, every eligible course in the term is a candidate
            (count mode).
        completed_courses: Course codes the student has already finished. Used
            both to exclude those courses and to satisfy prerequisite checks.
            Matching is case-insensitive.
        term: The DLSU term to scope the query to (e.g. ``'1261'``), so
            archived offerings are never mixed into a live schedule. Defaults
            to ``config.SCHEDULE_TERM``.

    Returns:
        Eligible sections as `Section` objects (already excluding completed
        courses and courses with an unmet prerequisite), before any
        time/day/gap filtering.
    """
    completed = [c.upper() for c in (completed_courses or [])]
    term = term or config.SCHEDULE_TERM
    where = ["term = ?"]
    params: list = [term]

    if desired_courses:
        placeholders = ",".join("?" for _ in desired_courses)
        where.append(f"UPPER(course_code) IN ({placeholders})")
        params.extend(code.upper() for code in desired_courses)

    if completed:
        placeholders = ",".join("?" for _ in completed)
        where.append(f"UPPER(course_code) NOT IN ({placeholders})")
        params.extend(completed)

    # Prerequisite eligibility: no prereq row pointing at a course the student
    # has NOT completed. A course with no prereq rows passes automatically.
    if completed:
        placeholders = ",".join("?" for _ in completed)
        prereq = (
            "NOT EXISTS (SELECT 1 FROM course_prerequisites cp "
            "WHERE cp.course_code = course_offerings.course_code "
            f"AND UPPER(cp.prerequisite_code) NOT IN ({placeholders}))"
        )
        params.extend(completed)
    else:
        prereq = (
            "NOT EXISTS (SELECT 1 FROM course_prerequisites cp "
            "WHERE cp.course_code = course_offerings.course_code)"
        )
    where.append(prereq)

    sql = (
        f"SELECT {_SCHEDULE_COLUMNS} FROM course_offerings "
        f"WHERE {' AND '.join(where)}"
    )
    with get_connection() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    return [_row_to_section(r) for r in rows]


def section_exists_in_db(section: Section, term: Optional[str] = None) -> bool:
    """C3 grounding check: confirm a chosen section is a real DB row.

    Args:
        section: The section to verify (matched by course code + section id).
        term: The term to look in; defaults to ``config.SCHEDULE_TERM``.

    Returns:
        True if a matching row exists in course_offerings for that term - i.e.
        the section was not hallucinated; False otherwise.
    """
    term = term or config.SCHEDULE_TERM
    sql = (
        "SELECT 1 FROM course_offerings "
        "WHERE course_code = ? AND section = ? AND term = ? LIMIT 1"
    )
    with get_connection() as conn:
        return conn.execute(
            sql, (section.course_code, section.section, term)
        ).fetchone() is not None


# --------------------------------------------------------------------------- #
# Correctness validator (RRL criteria C1-C6)
# --------------------------------------------------------------------------- #
def validate_schedule(chosen: list[Section], c: ScheduleConstraints,
                      check_db: bool = False, term: Optional[str] = None) -> dict:
    """
    Check a produced schedule against the correctness criteria from docs/RRL.md.
    Used by the test suite and can be surfaced in the final presentation.

    Args:
        chosen: The sections making up the schedule to validate.
        c: The constraints to validate against. Pass the *effective*
            constraints (``ScheduleResult.effective``) so relaxed schedules are
            judged against the constraints actually in force, not the original
            request.
        check_db: If True, also run C3 (verify every section is a real DB row).
            Left False in offline unit tests that use hand-built sections.
        term: Term to use for the C3 grounding check; defaults to
            ``config.SCHEDULE_TERM``. Ignored when ``check_db`` is False.

    Returns:
        A dict of per-criterion booleans - ``C1_no_overlap``,
        ``C2_no_duplicates``, ``C3_grounded``, ``C4_scope_faithful``,
        ``C5_constraints_hold`` - plus an overall ``correct`` flag that is the
        AND of all five.
    """
    # C1: no two chosen sections overlap in time.
    c1 = all(
        not sections_conflict(chosen[i], chosen[j])
        for i in range(len(chosen)) for j in range(i + 1, len(chosen))
    )
    # C2: at most one section per course (no duplicate course).
    codes = [s.course_code.upper() for s in chosen]
    c2 = len(codes) == len(set(codes))
    # C3: every chosen section is a real DB row (grounded, not hallucinated).
    c3 = all(section_exists_in_db(s, term) for s in chosen) if check_db else True
    # C4: only requested courses appear (scope faithful).
    if c.desired_courses:
        requested = {code.upper() for code in c.desired_courses}
        c4 = set(codes).issubset(requested)
    else:
        c4 = True  # count mode: any eligible course is in-scope by definition
    # C5: hard constraints hold (time window, allowed days, max/day).
    e, l = to_minutes(c.earliest), to_minutes(c.latest)
    days = set(c.allowed_days) if c.allowed_days else None
    c5_window = all(_passes_time_day(s, e, l, days) for s in chosen)
    c5_max = True
    if c.max_per_day is not None:
        for d in DAY_ORDER:
            if sum(1 for s in chosen if d in s.days) > c.max_per_day:
                c5_max = False
    c5 = c5_window and c5_max

    return {
        "C1_no_overlap": c1,
        "C2_no_duplicates": c2,
        "C3_grounded": c3,
        "C4_scope_faithful": c4,
        "C5_constraints_hold": c5,
        "correct": c1 and c2 and c3 and c4 and c5,
    }
