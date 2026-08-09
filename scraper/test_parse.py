"""Offline tests for the scraper's parsing layer (no browser needed)."""

import pytest

from scraper.parse import (
    build_offering,
    course_code_from_option,
    day_code,
    parse_schedules,
    to_24h,
)


@pytest.mark.parametrize("raw,expected", [
    ("11:00 AM", "11:00"),
    ("12:30 PM", "12:30"),
    ("12:00 AM", "00:00"),
    ("12:00 PM", "12:00"),
    ("1:00 PM", "13:00"),
    ("07:30 AM", "07:30"),
])
def test_to_24h(raw, expected):
    assert to_24h(raw) == expected


def test_day_codes():
    assert day_code("THURSDAY") == "H"
    assert day_code("saturday") == "S"
    assert day_code("Monday") == "M"


def test_course_code_split():
    assert course_code_from_option("GEWORLD - THE CONTEMPORARY WORLD") == "GEWORLD"
    assert course_code_from_option("GEETHIC") == "GEETHIC"


def test_hybrid_room_extracted_from_second_slot():
    o = build_offering(
        term="1261", course_code="GEWORLD", teacher="Ron Vilog", section="A58D",
        schedules_text="[ MONDAY - 11:00 AM - 12:30 PM : Online ] "
                       "[ THURSDAY - 11:00 AM - 12:30 PM : Room - L227 ]",
        remark="",
    )
    assert o.term == "1261"
    assert (o.sched1_day, o.sched2_day) == ("M", "H")
    assert o.room == "L227"
    assert o.remarks is None


def test_fully_online_has_no_room():
    slots = parse_schedules(
        "[ WEDNESDAY - 07:30 AM - 09:00 AM : Online ] "
        "[ SATURDAY - 07:30 AM - 09:00 AM : Online ]"
    )
    assert [s["day"] for s in slots] == ["W", "S"]
    assert all(s["room"] is None and s["online"] for s in slots)


def test_schedule_without_location():
    # Some courses (e.g. SAS1000) list no ": Online/Room" tail at all.
    o = build_offering(
        term="1261", course_code="SAS1000", teacher=None, section="A55D",
        schedules_text="[ WEDNESDAY - 01:30 PM - 03:00 PM ]",
    )
    assert (o.sched1_day, o.sched1_time_start, o.sched1_time_end) == ("W", "13:30", "15:00")
    assert o.room is None
    assert o.sched2_day is None


def test_single_schedule_pm():
    o = build_offering(
        term="1261", course_code="GEETHIC", teacher="X", section="Z01",
        schedules_text="[ FRIDAY - 12:45 PM - 02:15 PM : Room - V507 ]",
    )
    assert (o.sched1_time_start, o.sched1_time_end) == ("12:45", "14:15")
    assert o.sched2_day is None
    assert o.room == "V507"
