"""
Offline unit tests for the deterministic schedule solver (sql_agent/scheduler.py).

These need NO API key, NO DeepSeek call, and NO database - they exercise the
ILP, the infeasibility-relaxation loop, and the C1-C7 correctness validator on
hand-built sections, so they prove the anti-hallucination core in isolation.
The criteria they check are specified at the top of the validator section in
sql_agent/scheduler.py; the two catalog-backed ones (C3 grounding, C6
eligibility) live in test_scheduler_db.py because they need the real
course_offerings.db.

Each test below is named for the criterion it pins down, and every criterion has
both a positive case (the solver satisfies it) and a negative case (the
validator catches a schedule that violates it) - a validator that only ever says
"correct" proves nothing.

    pytest tests/test_scheduler.py -v
"""
from sql_agent.scheduler import (
    Meeting,
    ScheduleConstraints,
    ScheduleResult,
    Section,
    campus_span_minutes,
    grade_schedule,
    is_complete,
    section_wellformed,
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


# =========================================================================== #
# C1 - no overlapping timeslots
# =========================================================================== #
def test_conflict_detection():
    a = sec("A", "1", ("M", "09:00", "10:30"))
    b = sec("B", "1", ("M", "10:00", "11:30"))
    c = sec("C", "1", ("M", "10:30", "12:00"))  # touches a's end, no overlap
    assert sections_conflict(a, b)
    assert not sections_conflict(a, c)


def test_conflict_when_one_class_contains_another():
    # Fully-contained interval: the naive "starts inside" check misses this.
    a = sec("A", "1", ("W", "08:00", "12:00"))
    b = sec("B", "1", ("W", "09:00", "10:00"))
    assert sections_conflict(a, b)


def test_same_time_different_day_is_not_a_conflict():
    a = sec("A", "1", ("M", "09:00", "10:30"))
    b = sec("B", "1", ("T", "09:00", "10:30"))
    assert not sections_conflict(a, b)


def test_conflict_detected_on_second_meeting_only():
    # First meetings are fine; the twice-weekly second meetings collide. A
    # checker that only compared sched1 would call this schedule valid.
    a = sec("A", "1", ("M", "08:00", "09:30"), ("H", "13:00", "14:30"))
    b = sec("B", "1", ("T", "08:00", "09:30"), ("H", "14:00", "15:30"))
    assert sections_conflict(a, b)


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


def test_validator_flags_overlapping_schedule():
    c = ScheduleConstraints(desired_courses=["A", "B"])
    bad = [sec("A", "1", ("M", "09:00", "10:30")),
           sec("B", "1", ("M", "10:00", "11:30"))]
    report = validate_schedule(bad, c)
    assert report["C1_no_overlap"] is False
    assert report["correct"] is False
    assert report["failed"] == ["C1_no_overlap"]


# =========================================================================== #
# C2 - no duplicates
# =========================================================================== #
def test_solver_never_picks_two_sections_of_one_course():
    # Two GEWORLD sections that don't clash with each other - the solver must
    # still take only one, because a student can't enrol in a course twice.
    sections = [
        sec("GEWORLD", "1", ("M", "08:00", "09:00")),
        sec("GEWORLD", "2", ("M", "10:00", "11:00")),
    ]
    c = ScheduleConstraints(desired_courses=["GEWORLD"])
    r = solve_with_relaxation(sections, c)
    assert len(r.chosen) == 1
    assert validate_schedule(r.chosen, c)["C2_no_duplicates"]


def test_validator_flags_duplicate_course():
    c = ScheduleConstraints(desired_courses=["A"])
    bad = [sec("A", "1", ("M", "08:00", "09:00")),
           sec("A", "2", ("T", "08:00", "09:00"))]   # same course twice
    report = validate_schedule(bad, c)
    assert report["C2_no_duplicates"] is False
    assert report["correct"] is False


def test_validator_flags_same_section_listed_twice():
    # The repeated row overlaps itself, so this must fail C2 rather than being
    # waved through as "one course, listed once, no clash".
    c = ScheduleConstraints(desired_courses=["A"])
    bad = [sec("A", "1", ("M", "08:00", "09:00")),
           sec("A", "1", ("M", "08:00", "09:00"))]
    report = validate_schedule(bad, c)
    assert report["C2_no_duplicates"] is False


# =========================================================================== #
# C4 - scope faithful (the "asked for math, got physics" criterion)
# =========================================================================== #
def test_validator_flags_out_of_scope_course():
    c = ScheduleConstraints(desired_courses=["MATH"])
    bad = [sec("PHYS", "1", ("M", "09:00", "10:00"))]   # never requested
    report = validate_schedule(bad, c)
    assert report["C4_scope_faithful"] is False
    assert report["correct"] is False


def test_solver_ignores_courses_that_were_not_requested():
    # PHYS is in the pool and would fit perfectly - it must still not appear.
    sections = [
        sec("MATH", "1", ("M", "09:00", "10:30")),
        sec("PHYS", "1", ("T", "09:00", "10:30")),
    ]
    c = ScheduleConstraints(desired_courses=["MATH"])
    r = solve_with_relaxation(sections, c)
    assert {s.course_code for s in r.chosen} == {"MATH"}
    assert validate_schedule(r.chosen, c)["C4_scope_faithful"]


def test_count_mode_flags_section_outside_the_offered_pool():
    # Count mode has no requested list to compare against, so scope is checked
    # against the candidate pool instead: an invented section must not pass.
    pool = [sec("A", "1", ("M", "08:00", "09:00"))]
    c = ScheduleConstraints(desired_count=1)
    invented = [sec("A", "99", ("M", "08:00", "09:00"))]
    assert validate_schedule(invented, c)["C4_scope_faithful"] is True   # unchecked
    assert validate_schedule(invented, c, pool=pool)["C4_scope_faithful"] is False
    assert validate_schedule(pool, c, pool=pool)["C4_scope_faithful"] is True


# =========================================================================== #
# C5 - the hard constraints actually hold
# =========================================================================== #
def test_time_window_boundary_is_inclusive():
    # A class ending exactly at the boundary fits; one minute past does not.
    sections = [
        sec("A", "1", ("M", "13:00", "15:00")),   # ends exactly at latest
        sec("A", "2", ("M", "13:00", "15:01")),   # one minute too long
    ]
    c = ScheduleConstraints(desired_courses=["A"], earliest="09:00", latest="15:00")
    r = solve_with_relaxation(sections, c)
    assert [s.section for s in r.chosen] == ["1"]
    assert r.relaxations == []
    assert validate_schedule(r.chosen, c)["C5_constraints_hold"]


def test_allowed_days_respected_when_feasible():
    sections = [
        sec("A", "1", ("S", "09:00", "10:30")),   # Saturday - not allowed
        sec("A", "2", ("W", "09:00", "10:30")),   # allowed
    ]
    c = ScheduleConstraints(desired_courses=["A"], allowed_days=["M", "W", "F"])
    r = solve_with_relaxation(sections, c)
    assert [s.section for s in r.chosen] == ["2"]
    assert r.relaxations == []


def test_max_per_day_respected_when_feasible():
    # Both courses are offered on Tuesday too, but max 1/day forces the split.
    sections = [
        sec("A", "1", ("M", "08:00", "09:00")),
        sec("B", "1", ("M", "09:15", "10:15")),
        sec("B", "2", ("T", "09:15", "10:15")),
    ]
    c = ScheduleConstraints(desired_courses=["A", "B"], max_per_day=1)
    r = solve_with_relaxation(sections, c)
    assert {s.course_code for s in r.chosen} == {"A", "B"}
    assert r.relaxations == [], "a feasible request must not be relaxed"
    assert validate_schedule(r.chosen, c)["C5_constraints_hold"]


def test_validator_flags_time_window_violation():
    c = ScheduleConstraints(desired_courses=["A"], latest="15:00")
    bad = [sec("A", "1", ("M", "14:00", "16:00"))]
    report = validate_schedule(bad, c)
    assert report["C5_constraints_hold"] is False


def test_validator_flags_max_per_day_violation():
    c = ScheduleConstraints(desired_courses=["A", "B"], max_per_day=1)
    bad = [sec("A", "1", ("M", "08:00", "09:00")),
           sec("B", "1", ("M", "09:15", "10:15"))]
    report = validate_schedule(bad, c)
    assert report["C5_constraints_hold"] is False


def test_validator_flags_more_courses_than_requested_in_count_mode():
    # "Give me 2 GEs" must not come back with 3.
    c = ScheduleConstraints(desired_count=2)
    bad = [sec("A", "1", ("M", "08:00", "09:00")),
           sec("B", "1", ("M", "09:15", "10:15")),
           sec("C", "1", ("M", "10:30", "11:30"))]
    assert validate_schedule(bad, c)["C5_constraints_hold"] is False


# =========================================================================== #
# C7 - every section is actually attendable
# =========================================================================== #
def test_section_with_no_meetings_fails_wellformedness():
    # This is the shape an invented entry takes: a course code with no times.
    # It clashes with nothing, so C1 passes vacuously - C7 is what catches it.
    ghost = sec("PHYS", "1")
    c = ScheduleConstraints(desired_courses=["PHYS"])
    report = validate_schedule([ghost], c)
    assert report["C1_no_overlap"] is True
    assert report["C7_wellformed"] is False
    assert report["correct"] is False


def test_zero_length_and_reversed_intervals_fail_wellformedness():
    assert not section_wellformed(sec("A", "1", ("M", "09:00", "09:00")))
    assert not section_wellformed(sec("A", "1", ("M", "10:00", "09:00")))


def test_unknown_day_code_fails_wellformedness():
    assert not section_wellformed(sec("A", "1", ("X", "09:00", "10:00")))
    assert section_wellformed(sec("A", "1", ("U", "09:00", "10:00")))  # Sunday


def test_section_that_clashes_with_itself_fails_wellformedness():
    assert not section_wellformed(
        sec("A", "1", ("M", "09:00", "10:30"), ("M", "10:00", "11:30")))


# =========================================================================== #
# The relaxation / negotiation loop (correct-but-incomplete answers)
# =========================================================================== #
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

    # The point of the criteria split: this answer is CORRECT but INCOMPLETE,
    # and that is a success because the gap is reported.
    grade = grade_schedule(r, c)
    assert grade["correct"] is True
    assert grade["complete"] is False
    assert grade["transparent"] is True
    assert grade["outcome"] == "NEGOTIATED"
    assert grade["coverage"] == 0.5


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


def test_relaxation_order_gives_up_the_cheapest_constraint_first():
    # Both max_per_day and the day restriction block this request. The loop
    # must try raising max/day first and stop there - never volunteer more of
    # the student's preferences than it has to.
    sections = [
        sec("A", "1", ("M", "08:00", "09:00")),
        sec("B", "1", ("M", "09:15", "10:15")),
    ]
    c = ScheduleConstraints(desired_courses=["A", "B"],
                            allowed_days=["M"], max_per_day=1)
    r = solve_with_relaxation(sections, c)
    assert {s.course_code for s in r.chosen} == {"A", "B"}
    assert len(r.relaxations) == 1
    assert "classes on a single day" in r.relaxations[0]
    assert r.effective.allowed_days == ["M"], "day limit should still be in force"


def test_relaxation_is_recorded_in_effective_constraints():
    # Correctness is judged against `effective`, so it must reflect what was
    # loosened - otherwise a relaxed schedule would be scored as a violation.
    sections = [sec("A", "1", ("M", "18:00", "19:30"))]
    c = ScheduleConstraints(desired_courses=["A"], latest="15:00")
    r = solve_with_relaxation(sections, c)
    assert r.effective.latest is None
    assert validate_schedule(r.chosen, r.effective)["C5_constraints_hold"] is True
    assert validate_schedule(r.chosen, c)["C5_constraints_hold"] is False


def test_satisfiable_request_reports_a_clean_full_success():
    sections = [
        sec("A", "1", ("M", "08:00", "09:00")),
        sec("B", "1", ("T", "08:00", "09:00")),
    ]
    c = ScheduleConstraints(desired_courses=["A", "B"])
    r = solve_with_relaxation(sections, c)
    grade = grade_schedule(r, c)
    assert grade["outcome"] == "FULL"
    assert grade["complete"] and grade["correct"] and not grade["negotiated"]
    assert grade["coverage"] == 1.0


def test_grade_fails_a_schedule_that_hides_a_dropped_course():
    # A result that silently omits a requested course (empty dropped_courses)
    # is a FAILURE even though every section in it is individually fine.
    chosen = [sec("A", "1", ("M", "08:00", "09:00"))]
    c = ScheduleConstraints(desired_courses=["A", "B"])
    dishonest = ScheduleResult(feasible=True, chosen=chosen,
                               dropped_courses=[], effective=c)
    grade = grade_schedule(dishonest, c)
    assert grade["correct"] is True          # nothing in it is wrong...
    assert grade["transparent"] is False     # ...but it hid the gap
    assert grade["outcome"] == "FAILED"


def test_grade_fails_an_incorrect_schedule():
    clashing = [sec("A", "1", ("M", "08:00", "09:30")),
                sec("B", "1", ("M", "09:00", "10:00"))]
    c = ScheduleConstraints(desired_courses=["A", "B"])
    bad = ScheduleResult(feasible=True, chosen=clashing, effective=c)
    grade = grade_schedule(bad, c)
    assert grade["complete"] is True         # it has everything asked for...
    assert grade["correct"] is False         # ...but it is unattendable
    assert grade["outcome"] == "FAILED"


# =========================================================================== #
# Count mode ("give me N subjects")
# =========================================================================== #
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
    assert is_complete(r.chosen, c)


def test_count_mode_reports_the_shortfall_instead_of_padding():
    # Asked for 3, but everything on offer collides - only 2 can be taken.
    sections = [
        sec("A", "1", ("M", "08:00", "10:00")),
        sec("B", "1", ("M", "09:00", "11:00")),   # clashes with A
        sec("C", "1", ("M", "10:30", "11:30")),   # clashes with B, fits A
    ]
    c = ScheduleConstraints(desired_count=3)
    r = solve_with_relaxation(sections, c)
    assert len({s.course_code for s in r.chosen}) == 2
    assert r.unfilled_count == 1, "the missing subject must be reported"
    grade = grade_schedule(r, c)
    assert grade["correct"] and grade["transparent"]
    assert grade["complete"] is False
    assert grade["outcome"] == "NEGOTIATED"


# =========================================================================== #
# Soft objective: minimise campus time / gaps (quality, not correctness)
# =========================================================================== #
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


def test_compactness_never_costs_a_course():
    # Dropping C would give a far more compact schedule. Course count must
    # strictly dominate the compactness objective, so all three are kept.
    sections = [
        sec("A", "1", ("M", "08:00", "09:00")),
        sec("B", "1", ("M", "09:15", "10:15")),
        sec("C", "1", ("M", "19:00", "20:00")),   # late, wrecks compactness
    ]
    c = ScheduleConstraints(desired_courses=["A", "B", "C"], no_gaps=True)
    r = solve_with_relaxation(sections, c)
    assert {s.course_code for s in r.chosen} == {"A", "B", "C"}
    assert is_complete(r.chosen, c)


def test_campus_span_sums_per_day_and_ignores_free_days():
    chosen = [
        sec("A", "1", ("M", "08:00", "09:00"), ("W", "08:00", "09:00")),
        sec("B", "1", ("M", "10:00", "11:00")),
    ]
    # Monday 08:00-11:00 = 180, Wednesday 08:00-09:00 = 60, other days 0.
    assert campus_span_minutes(chosen) == 240


# =========================================================================== #
# Determinism - the property an LLM-built timetable cannot offer
# =========================================================================== #
def test_same_request_produces_the_same_schedule():
    sections = [
        sec("A", "1", ("M", "08:00", "09:00")),
        sec("A", "2", ("T", "08:00", "09:00")),
        sec("B", "1", ("M", "09:15", "10:15")),
        sec("B", "2", ("H", "09:15", "10:15")),
    ]
    c = ScheduleConstraints(desired_courses=["A", "B"], no_gaps=True)
    first = solve_with_relaxation(sections, c)
    for _ in range(3):
        again = solve_with_relaxation(sections, c)
        assert ([(s.course_code, s.section) for s in first.chosen]
                == [(s.course_code, s.section) for s in again.chosen])


# =========================================================================== #
# Empty / degenerate inputs
# =========================================================================== #
def test_empty_pool_fails_honestly_rather_than_inventing():
    c = ScheduleConstraints(desired_courses=["A", "B"])
    r = solve_with_relaxation([], c)
    assert r.feasible is False
    assert r.chosen == []
    assert r.dropped_courses == ["A", "B"]      # both reported, nothing faked
    assert grade_schedule(r, c)["outcome"] == "FAILED"


def test_unavailable_course_is_reported_not_substituted():
    # The student asks for two courses but only one exists in the catalog.
    sections = [sec("A", "1", ("M", "08:00", "09:00"))]
    c = ScheduleConstraints(desired_courses=["A", "B"])
    r = solve_with_relaxation(sections, c)
    assert r.dropped_courses == ["B"]
    assert {s.course_code for s in r.chosen} == {"A"}
    assert validate_schedule(r.chosen, c)["correct"]
