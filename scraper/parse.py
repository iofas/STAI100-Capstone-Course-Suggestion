"""Pure parsing helpers for ArchersHub Course Finder rows.

These functions contain no Playwright / network code so they can be unit
tested offline against strings copied straight from the site's DOM.

Target schema (course_offerings table):
    course_code, teacher, section,
    sched1_day, sched1_time_start, sched1_time_end,
    sched2_day, sched2_time_start, sched2_time_end,
    room, remarks
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict

# ArchersHub spells days out; the DB uses single-letter codes.
# Note the non-obvious ones: THURSDAY -> H, SATURDAY -> S, SUNDAY -> U.
DAY_MAP = {
    "MONDAY": "M",
    "TUESDAY": "T",
    "WEDNESDAY": "W",
    "THURSDAY": "H",
    "FRIDAY": "F",
    "SATURDAY": "S",
    "SUNDAY": "U",
}

# One schedule bracket. The ": location" tail is optional - some courses list
# no room/online tag at all:
#   [ MONDAY - 11:00 AM - 12:30 PM : Online ]
#   [ THURSDAY - 11:00 AM - 12:30 PM : Room - L227 ]
#   [ WEDNESDAY - 01:30 PM - 03:00 PM ]              (no location)
_SCHED_RE = re.compile(
    r"\[\s*"
    r"(?P<day>[A-Za-z]+)\s*-\s*"
    r"(?P<start>\d{1,2}:\d{2}\s*[AaPp][Mm])\s*-\s*"
    r"(?P<end>\d{1,2}:\d{2}\s*[AaPp][Mm])\s*"
    r"(?::\s*(?P<loc>[^\]]+?)\s*)?"
    r"\]"
)


@dataclass
class Offering:
    term: str
    course_code: str
    teacher: str | None
    section: str
    sched1_day: str
    sched1_time_start: str
    sched1_time_end: str
    sched2_day: str | None
    sched2_time_start: str | None
    sched2_time_end: str | None
    room: str | None
    remarks: str | None

    def as_dict(self) -> dict:
        return asdict(self)


def to_24h(t: str) -> str:
    """'11:00 AM' -> '11:00', '12:30 PM' -> '12:30', '1:00 PM' -> '13:00'."""
    t = t.strip().upper().replace(" ", "")
    m = re.match(r"^(\d{1,2}):(\d{2})(AM|PM)$", t)
    if not m:
        raise ValueError(f"Unrecognised time: {t!r}")
    hh, mm, ap = int(m.group(1)), m.group(2), m.group(3)
    if ap == "AM":
        if hh == 12:
            hh = 0
    else:  # PM
        if hh != 12:
            hh += 12
    return f"{hh:02d}:{mm}"


def day_code(word: str) -> str:
    """'THURSDAY' -> 'H'. Raises on anything unexpected."""
    key = word.strip().upper()
    if key not in DAY_MAP:
        raise ValueError(f"Unrecognised day: {word!r}")
    return DAY_MAP[key]


def course_code_from_option(option_text: str) -> str:
    """'GEWORLD - THE CONTEMPORARY WORLD' -> 'GEWORLD'."""
    return option_text.split(" - ", 1)[0].strip()


def _parse_location(loc: str) -> tuple[str | None, bool]:
    """Return (room_or_None, is_online) from a schedule location fragment.

    'Online'        -> (None, True)
    'Room - L227'   -> ('L227', False)
    'L227'          -> ('L227', False)
    """
    loc = loc.strip()
    if re.fullmatch(r"(?i)online", loc):
        return None, True
    m = re.match(r"(?i)^room\s*-\s*(.+)$", loc)
    if m:
        return m.group(1).strip(), False
    return loc or None, False


def parse_schedules(cell_text: str) -> list[dict]:
    """Parse a Schedules cell (may hold 1-2 bracketed lines) into slots.

    Each slot: {day, start, end, room, online}.
    """
    slots: list[dict] = []
    for m in _SCHED_RE.finditer(cell_text):
        loc = m.group("loc")
        room, online = _parse_location(loc) if loc is not None else (None, False)
        slots.append(
            {
                "day": day_code(m.group("day")),
                "start": to_24h(m.group("start")),
                "end": to_24h(m.group("end")),
                "room": room,
                "online": online,
            }
        )
    return slots


def build_offering(
    *,
    term: str,
    course_code: str,
    teacher: str | None,
    section: str,
    schedules_text: str,
    remark: str | None = None,
) -> Offering:
    """Assemble one Offering row from raw scraped cell values.

    - Days/times are normalised to the DB's codes/24h format.
    - `room` is the physical room from whichever slot has one (hybrid courses
      list an Online slot + a Room slot); fully-online courses -> None.
    """
    slots = parse_schedules(schedules_text)
    if not slots:
        raise ValueError(f"No schedule parsed from: {schedules_text!r}")

    s1 = slots[0]
    s2 = slots[1] if len(slots) > 1 else None

    # Physical room = first non-None room across the slots.
    room = next((s["room"] for s in slots if s["room"]), None)

    remarks = (remark or "").strip() or None

    return Offering(
        term=term.strip(),
        course_code=course_code.strip(),
        teacher=(teacher or "").strip() or None,
        section=section.strip(),
        sched1_day=s1["day"],
        sched1_time_start=s1["start"],
        sched1_time_end=s1["end"],
        sched2_day=s2["day"] if s2 else None,
        sched2_time_start=s2["start"] if s2 else None,
        sched2_time_end=s2["end"] if s2 else None,
        room=room,
        remarks=remarks,
    )
