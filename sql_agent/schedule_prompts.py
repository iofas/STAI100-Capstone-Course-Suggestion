"""
Prompt design for the Schedule Agent (Step 1: NL -> structured constraints).

The model here does exactly ONE job: translate the student's request into a
JSON constraint object. It never builds the timetable and never emits SQL - the
deterministic ILP solver (scheduler.py) does that. Keeping the model on
translation duty only is what stops it from hallucinating a schedule.
"""
import json

CONSTRAINT_SYSTEM_TEMPLATE = """You are the intake step of a university course-scheduling assistant. \
Your ONLY job is to read a student's message and extract structured scheduling \
constraints as JSON. You do NOT build schedules and you do NOT write SQL - a \
separate deterministic solver does that.

Valid course codes (a student may type these, or a course's plain name; map to \
the code, uppercase): {course_codes}

Decide first whether the student wants you to BUILD A SCHEDULE / TIMETABLE - i.e. \
fit several courses together without clashes, or "give me N subjects", "plan my \
week", "make me a schedule", "which sections fit together". A plain single-fact \
lookup ("what sections of X are on Monday?", "who teaches Y?") is NOT a schedule \
request - set is_schedule_request to false for those.

Extract these fields:
- is_schedule_request (bool): true only for build-a-timetable requests.
- desired_courses (list of uppercase codes): the specific courses the student \
named to include. Empty if they only gave a count.
- desired_count (int or null): if they asked for a NUMBER of subjects \
("give me 5 GE subjects") rather than naming them, put the number here.
- completed_courses (list of uppercase codes): courses they said they already \
took/passed (used to exclude them and enforce prerequisites).
- earliest (string "HH:MM" 24-hour, or null): no class may START before this.
- latest (string "HH:MM" 24-hour, or null): no class may END after this.
- allowed_days (list from ["M","T","W","H","F","S"], or null): restrict which \
days the student is willing to be on campus. M=Mon T=Tue W=Wed H=Thu F=Fri \
S=Sat. Null means any day is fine.
- max_per_day (int or null): the most classes the student will take in one day.
- no_gaps (bool): true if they want to minimise time spent on campus / avoid \
gaps between classes / a compact/packed schedule.
- reasoning (string): one or two sentences on how you read the request.

Rules:
1. Normalise all times to 24-hour "HH:MM" (e.g. "3pm" -> "15:00", "12:30pm" -> \
"12:30", "9" in a morning context -> "09:00").
2. Only include a field's value if the student actually expressed it; otherwise \
use null (or an empty list / false). Do NOT invent constraints.
3. CONTEXT RETENTION: on a follow-up, carry over all constraints still in force \
from earlier turns unless the student changes or removes them. If the student \
says e.g. "actually drop GEWORLD", remove it from desired_courses.
4. Never put a code in desired_courses that isn't in the valid list above; if a \
named course isn't recognisable, leave it out and note it in reasoning.

Respond with ONLY a JSON object with exactly these keys: is_schedule_request, \
desired_courses, desired_count, completed_courses, earliest, latest, \
allowed_days, max_per_day, no_gaps, reasoning. No markdown, no extra text."""


_FEW_SHOT = [
    {
        "q": "Build me a schedule with GEARTAP, GEWORLD and LCFAITH, nothing "
             "before 9am or after 3pm, and I hate gaps - pack my days. Max 2 "
             "classes a day.",
        "a": {
            "is_schedule_request": True,
            "desired_courses": ["GEARTAP", "GEWORLD", "LCFAITH"],
            "desired_count": None,
            "completed_courses": [],
            "earliest": "09:00",
            "latest": "15:00",
            "allowed_days": None,
            "max_per_day": 2,
            "no_gaps": True,
            "reasoning": "Named three courses to fit together with a 09:00-15:00 "
                         "window, a compact (no-gaps) preference, and a 2/day cap.",
        },
    },
    {
        "q": "Give me 4 GE subjects I can take on only Mondays and Wednesdays. "
             "I've already passed GEUSELF.",
        "a": {
            "is_schedule_request": True,
            "desired_courses": [],
            "desired_count": 4,
            "completed_courses": ["GEUSELF"],
            "earliest": None,
            "latest": None,
            "allowed_days": ["M", "W"],
            "max_per_day": None,
            "no_gaps": False,
            "reasoning": "Count-mode request for 4 subjects restricted to Mon/Wed, "
                         "excluding the already-completed GEUSELF.",
        },
    },
    {
        "q": "What sections of GEARTAP are taught on Mondays?",
        "a": {
            "is_schedule_request": False,
            "desired_courses": [],
            "desired_count": None,
            "completed_courses": [],
            "earliest": None,
            "latest": None,
            "allowed_days": None,
            "max_per_day": None,
            "no_gaps": False,
            "reasoning": "A single-fact lookup about one course, not a request to "
                         "build a timetable.",
        },
    },
]


def build_constraint_messages(course_codes: list[str], question: str,
                              history: list[dict] = None) -> list[dict]:
    system = CONSTRAINT_SYSTEM_TEMPLATE.format(course_codes=", ".join(course_codes))
    messages = [{"role": "system", "content": system}]
    for ex in _FEW_SHOT:
        messages.append({"role": "user", "content": ex["q"]})
        messages.append({"role": "assistant", "content": json.dumps(ex["a"])})
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": question})
    return messages
