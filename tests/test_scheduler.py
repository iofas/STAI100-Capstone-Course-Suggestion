"""
Offline unit tests for the deterministic schedule solver (sql_agent/scheduler.py).

These need NO API key, NO DeepSeek call, and NO database - they exercise the
ILP, the infeasibility-relaxation loop, and the C1-C6 correctness validator on
hand-built sections, so they prove the anti-hallucination core in isolation.

    pytest test_scheduler.py -v
"""
from sql_agent.scheduler import (
    Meeting,
    ScheduleConstraints,
    Section,
    campus_span_minutes,
    sections_conflict,
    solve_with_relaxation,
    validate_schedule,
)


def sec(code, section, *meetings):
    """meetings: (day, 'HH:MM', 'HH:MM') tuples."""
    ms = []
    for day, start, end in meetings:
        sh, sm = start.split(":")
        eh, em = end.split(":")
        ms.append(Meeting(day, int(sh) * 60 + int(sm), int(eh) * 60 + int(em)))
    return Section(course_code=code, section=section, meetings=ms)


# --- primitives ------------------------------------------------------------
def test_conflict_detection():
    a = sec("A", "1", ("M", "09:00", "10:30"))
    b = sec("B", "1", ("M", "10:00", "11:30"))
    c = sec("C", "1", ("M", "10:30", "12:00"))  # touches a's end, no overlap
    assert sections_conflict(a, b)
    assert not sections_conflict(a, c)


# --- C1: feasible, conflict-free -------------------------------------------
def test_picks_non_conflicting_sections():
    sections = [
        sec("GEARTAP", "1", ("M", "09:15", "10:45")),
        sec("GEWORLD", "2", ("M", "10:00", "11:30")),   # clashes w/ GEARTAP-1
        sec("GEWORLD", "5", ("M", "11:00", "12:30")),   # fits
        sec("LCFAITH", "3", ("M", "13:00", "14:30")),
    ]
    c = ScheduleConstraints(desired_courses=["GEARTAP", "GEWORLD", "LCFAITH"])
    r = solve_with_relaxation(sections, c)
    assert r.feasible
    assert {s.course_code for s in r.chosen} == {"GEARTAP", "GEWORLD", "LCFAITH"}
    assert r.relaxations == [] and r.dropped_courses == []
    assert validate_schedule(r.chosen, c)["correct"]
    # the GEWORLD section chosen must be the non-conflicting one
    assert next(s for s in r.chosen if s.course_code == "GEWORLD").section == "5"


# --- C2 / C3 / C4 via validator on a genuinely infeasible pair -------------
def test_drops_course_when_truly_infeasible():
    # Two required courses only offered at the exact same single slot.
    sections = [
        sec("MATH", "1", ("M", "09:00", "10:30")),
        sec("PHYS", "1", ("M", "09:00", "10:30")),
    ]
    c = ScheduleConstraints(desired_courses=["MATH", "PHYS"])
    r = solve_with_relaxation(sections, c)
    assert len(r.chosen) == 1                    # can't fit both
    assert len(r.dropped_courses) == 1           # honestly reported, not faked
    assert validate_schedule(r.chosen, c)["C1_no_overlap"]


# --- Fix #2: the relaxation / negotiation loop -----------------------------
def test_relaxes_max_per_day_when_needed():
    # Three courses, all only on Monday; max_per_day=1 makes it infeasible.
    sections = [
        sec("A", "1", ("M", "08:00", "09:00")),
        sec("B", "1", ("M", "09:15", "10:15")),
        sec("C", "1", ("M", "10:30", "11:30")),
    ]
    c = ScheduleConstraints(desired_courses=["A", "B", "C"], max_per_day=1)
    r = solve_with_relaxation(sections, c)
    assert r.feasible
    assert {s.course_code for s in r.chosen} == {"A", "B", "C"}
    assert r.relaxations, "should have recorded a max-per-day relaxation"
    assert any("classes on a single day" in note for note in r.relaxations)


def test_relaxes_time_window_when_needed():
    sections = [
        sec("A", "1", ("M", "08:00", "09:30")),
        sec("B", "1", ("T", "16:00", "17:30")),   # outside a 09:00-15:00 window
    ]
    c = ScheduleConstraints(desired_courses=["A", "B"],
                            earliest="09:00", latest="15:00")
    r = solve_with_relaxation(sections, c)
    assert r.feasible
    assert {s.course_code for s in r.chosen} == {"A", "B"}
    assert any("class-time window" in note for note in r.relaxations)


# --- soft objective: minimise campus time / gaps ---------------------------
def test_no_gaps_prefers_compact_schedule():
    # B1 (right after A) vs B2 (big gap after A). no_gaps must pick B1.
    sections = [
        sec("A", "1", ("M", "08:00", "09:00")),
        sec("B", "1", ("M", "09:15", "10:15")),   # compact
        sec("B", "2", ("M", "15:00", "16:00")),   # leaves a huge gap
    ]
    c = ScheduleConstraints(desired_courses=["A", "B"], no_gaps=True)
    r = solve_with_relaxation(sections, c)
    chosen_b = next(s for s in r.chosen if s.course_code == "B")
    assert chosen_b.section == "1"
    assert campus_span_minutes(r.chosen) == 135   # 08:00 -> 10:15


# --- count mode: "give me N subjects" --------------------------------------
def test_count_mode_picks_n_non_conflicting():
    sections = [
        sec("A", "1", ("M", "08:00", "09:00")),
        sec("B", "1", ("M", "09:15", "10:15")),
        sec("C", "1", ("M", "10:30", "11:30")),
        sec("D", "1", ("M", "08:30", "09:30")),   # conflicts with A and B
    ]
    c = ScheduleConstraints(desired_count=3, no_gaps=True)
    r = solve_with_relaxation(sections, c)
    assert len({s.course_code for s in r.chosen}) == 3
    assert validate_schedule(r.chosen, c)["C1_no_overlap"]


# --- validator catches a bad (hallucinated-scope) schedule -----------------
def test_validator_flags_out_of_scope_course():
    c = ScheduleConstraints(desired_courses=["MATH"])
    bad = [sec("PHYS", "1", ("M", "09:00", "10:00"))]   # never requested
    report = validate_schedule(bad, c)
    assert report["C4_scope_faithful"] is False
    assert report["correct"] is False
