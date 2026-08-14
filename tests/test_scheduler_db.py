"""
Catalog-backed correctness tests for the scheduler (criteria C3 and C6).

These are the two criteria that cannot be checked on hand-built data, because
they are about agreement with the real catalog:

    C3 grounded  - every section in a schedule is a real course_offerings row,
                   with the days/times the catalog actually lists for it.
    C6 eligible  - no course the student already took, and none with an unmet
                   prerequisite.

They read course_offerings.db but make NO API call, so they run offline like
tests/test_scheduler.py. Everything is scoped to a single term, since the
database holds several and mixing them is itself a correctness failure.

    pytest tests/test_scheduler_db.py -v
"""
import pytest

from sql_agent.scheduler import (
    Meeting,
    ScheduleConstraints,
    Section,
    fetch_eligible_sections,
    grade_schedule,
    section_exists_in_db,
    section_grounded,
    solve_with_relaxation,
    unmet_prerequisites,
    validate_schedule,
)

TERM = "1261"          # current term; 1241 is the archived one
OTHER_TERM = "1241"


@pytest.fixture(scope="module")
def catalog() -> list[Section]:
    """Every eligible section in the current term (no completed courses)."""
    sections = fetch_eligible_sections(None, [], term=TERM)
    if not sections:
        pytest.skip("course_offerings.db has no rows for the current term")
    return sections


def a_section(catalog: list[Section], code: str) -> Section:
    for s in catalog:
        if s.course_code == code and s.meetings:
            return s
    pytest.skip(f"{code} is not offered in term {TERM}")


# =========================================================================== #
# C3 - grounded: real section, real times
# =========================================================================== #
def test_real_section_is_grounded(catalog):
    assert section_grounded(a_section(catalog, "GEARTAP"), term=TERM)


def test_invented_section_id_is_not_grounded(catalog):
    real = a_section(catalog, "GEARTAP")
    fake = Section(course_code=real.course_code, section="ZZ99",
                   meetings=real.meetings)
    assert section_grounded(fake, term=TERM) is False


def test_invented_course_code_is_not_grounded():
    fake = Section(course_code="GEQUANTUM", section="X01",
                   meetings=[Meeting("M", 540, 630)])
    assert section_grounded(fake, term=TERM) is False


def test_real_section_with_invented_times_is_not_grounded(catalog):
    # The subtle hallucination: a genuine section id, moved to a slot that
    # suits the request. Existence alone would wave this through, which is why
    # C3 compares the meetings too.
    real = a_section(catalog, "GEWORLD")
    moved = Section(course_code=real.course_code, section=real.section,
                    meetings=[Meeting("S", 8 * 60, 9 * 60)])
    assert section_exists_in_db(moved, term=TERM) is True
    assert section_grounded(moved, term=TERM) is False


def test_section_from_another_term_is_not_grounded_in_this_one(catalog):
    archived = fetch_eligible_sections(None, [], term=OTHER_TERM)
    if not archived:
        pytest.skip("no archived-term rows to test against")
    stale = next((s for s in archived
                  if not section_exists_in_db(s, term=TERM)), None)
    if stale is None:
        pytest.skip("both terms offer the same sections")
    assert section_grounded(stale, term=TERM) is False


def test_solved_schedule_is_fully_grounded():
    wanted = ["GEARTAP", "GEWORLD", "LCFAITH"]
    sections = fetch_eligible_sections(wanted, [], term=TERM)
    if not sections:
        pytest.skip("requested courses are not offered in this term")
    result = solve_with_relaxation(sections, ScheduleConstraints(desired_courses=wanted))
    report = validate_schedule(result.chosen, result.effective,
                               check_db=True, term=TERM, pool=sections)
    assert report["C3_grounded"] is True
    assert report["correct"] is True, report["failed"]


# =========================================================================== #
# C6 - eligible: prerequisites met, nothing already taken
# =========================================================================== #
def test_unmet_prerequisite_is_reported():
    # LCLSTWO requires LCLSONE (see course_prerequisites).
    assert unmet_prerequisites("LCLSTWO", []) == ["LCLSONE"]
    assert unmet_prerequisites("LCLSTWO", ["LCLSONE"]) == []
    assert unmet_prerequisites("LCLSTWO", ["lclsone"]) == []   # case-insensitive


def test_course_without_prerequisites_is_always_eligible():
    assert unmet_prerequisites("GEARTAP", []) == []


def test_course_with_unmet_prerequisite_is_never_offered():
    # Asking for LCLSTWO without LCLSONE must yield nothing to schedule, not a
    # schedule the student cannot actually enrol in.
    sections = fetch_eligible_sections(["LCLSTWO"], [], term=TERM)
    assert sections == []


def test_prerequisite_unlocks_once_completed():
    sections = fetch_eligible_sections(["LCLSTWO"], ["LCLSONE"], term=TERM)
    if not sections:
        pytest.skip("LCLSTWO is not offered in this term")
    assert all(s.course_code == "LCLSTWO" for s in sections)


def test_completed_course_is_never_suggested():
    sections = fetch_eligible_sections(None, ["GEARTAP"], term=TERM)
    assert all(s.course_code != "GEARTAP" for s in sections)


def test_validator_flags_a_course_the_student_already_took():
    c = ScheduleConstraints(desired_courses=["GEARTAP"], completed_courses=["GEARTAP"])
    taken = fetch_eligible_sections(["GEARTAP"], [], term=TERM)
    if not taken:
        pytest.skip("GEARTAP is not offered in this term")
    report = validate_schedule(taken[:1], c, check_db=True, term=TERM)
    assert report["C6_eligible"] is False
    assert report["correct"] is False


def test_validator_flags_an_unmet_prerequisite():
    c = ScheduleConstraints(desired_courses=["LCLSTWO"])   # LCLSONE not taken
    offered = fetch_eligible_sections(["LCLSTWO"], ["LCLSONE"], term=TERM)
    if not offered:
        pytest.skip("LCLSTWO is not offered in this term")
    report = validate_schedule(offered[:1], c, check_db=True, term=TERM)
    assert report["C6_eligible"] is False


# =========================================================================== #
# End-to-end on real data: correct, and honest when it can't be complete
# =========================================================================== #
def test_real_request_is_correct_and_complete():
    wanted = ["GEARTAP", "GEWORLD", "LCFAITH", "GERIZAL"]
    sections = fetch_eligible_sections(wanted, [], term=TERM)
    if not sections:
        pytest.skip("requested courses are not offered in this term")
    c = ScheduleConstraints(desired_courses=wanted, no_gaps=True)
    result = solve_with_relaxation(sections, c)
    grade = grade_schedule(result, c, check_db=True, term=TERM, pool=sections)
    assert grade["correct"] is True, grade["failed"]
    assert grade["complete"] is True
    assert grade["outcome"] == "FULL"


def test_impossible_window_is_negotiated_not_faked():
    # Nothing is offered 06:00-07:00, so the window has to give. The result
    # must stay correct against the relaxed constraints and say what it did.
    wanted = ["GEARTAP", "GEWORLD"]
    sections = fetch_eligible_sections(wanted, [], term=TERM)
    if not sections:
        pytest.skip("requested courses are not offered in this term")
    c = ScheduleConstraints(desired_courses=wanted, earliest="06:00", latest="07:00")
    result = solve_with_relaxation(sections, c)
    grade = grade_schedule(result, c, check_db=True, term=TERM, pool=sections)
    assert result.relaxations, "an impossible window must be reported, not ignored"
    assert grade["correct"] is True, grade["failed"]
    assert grade["outcome"] == "NEGOTIATED"


def test_every_catalog_section_is_wellformed(catalog):
    # C7 as a data-quality check over the whole term: if a real row can't be
    # attended as parsed, the scraper or the parser is at fault, not the solver.
    from sql_agent.scheduler import section_wellformed
    broken = [f"{s.course_code} {s.section}" for s in catalog
              if not section_wellformed(s)]
    assert not broken, f"unattendable catalog rows: {broken[:10]}"
