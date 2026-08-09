"""
Schedule Agent orchestration - the agentic layer that ties Step 1 (LLM
translation) to Steps 2-3 (deterministic retrieval + ILP scheduling), and
handles the infeasibility NEGOTIATION described in docs/RRL.md (Fix #2).

Flow of ``respond()`` (the single entry point the API/UI calls):

    natural language
        -> extract_constraints()   [1 LLM call: NL -> constraint JSON, or "this
                                     is a plain lookup, not a schedule request"]
        -> if schedule request:
              fetch_eligible_sections()      [deterministic SQL]
              solve_with_relaxation()        [CP-SAT ILP + relaxation loop]
              -> a verified schedule, or an honest, negotiated best-effort
           else:
              fall back to sql_agent.ask()   [the existing lookup path]

The LLM is never asked to produce the timetable, so it cannot hallucinate one.
"""
from __future__ import annotations

import json
from typing import Optional

import mlflow
from pydantic import BaseModel, ValidationError

from .agent import _get_client, ask
from .config import DEEPSEEK_MODEL
from .db import get_connection
from .scheduler import (
    DAY_NAMES,
    DAY_ORDER,
    ScheduleConstraints,
    ScheduleResult,
    Section,
    fetch_eligible_sections,
    minutes_to_hhmm,
    solve_with_relaxation,
    validate_schedule,
)


class _ConstraintModel(BaseModel):
    is_schedule_request: bool = False
    desired_courses: list[str] = []
    desired_count: Optional[int] = None
    completed_courses: list[str] = []
    earliest: Optional[str] = None
    latest: Optional[str] = None
    allowed_days: Optional[list[str]] = None
    max_per_day: Optional[int] = None
    no_gaps: bool = False
    reasoning: str = ""


def _valid_course_codes() -> list[str]:
    with get_connection() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT course_code FROM course_offerings ORDER BY course_code"
        )]


def extract_constraints(question: str, history: list[dict] = None):
    """One LLM call: natural language -> (_ConstraintModel, raw_json_string).

    This is the only place the model is involved in the scheduling path.
    """
    from .schedule_prompts import build_constraint_messages

    messages = build_constraint_messages(_valid_course_codes(), question, history)
    response = _get_client().chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=messages,
        temperature=0,
        response_format={"type": "json_object"},
        extra_body={"thinking": {"type": "disabled"}},
    )
    raw = response.choices[0].message.content
    try:
        parsed = _ConstraintModel.model_validate_json(raw)
    except ValidationError:
        parsed = _ConstraintModel(is_schedule_request=False,
                                  reasoning="Could not parse constraints.")
    return parsed, raw


# --------------------------------------------------------------------------- #
# Rendering the result as a human-readable, honest explanation
# --------------------------------------------------------------------------- #
def _fmt_meetings(section: Section) -> str:
    return "; ".join(
        f"{DAY_NAMES.get(m.day, m.day)} {minutes_to_hhmm(m.start)}-{minutes_to_hhmm(m.end)}"
        for m in section.meetings
    )


def _schedule_rows(result: ScheduleResult) -> list[dict]:
    """Timetable as display rows (also what the UI dataframe renders)."""
    rows = []
    for s in sorted(result.chosen, key=lambda s: (min((m.day for m in s.meetings),
                    key=lambda d: DAY_ORDER.index(d)) if s.meetings else "Z")):
        rows.append({
            "Course": s.course_code,
            "Section": s.section,
            "Schedule": _fmt_meetings(s),
            "Room": s.raw.get("room"),
            "Teacher": s.raw.get("teacher"),
            "Remarks": s.raw.get("remarks"),
        })
    return rows


def _render_explanation(c: ScheduleConstraints, result: ScheduleResult,
                        report: dict) -> str:
    lines: list[str] = []
    n = len(result.chosen)

    if not result.chosen:
        return ("I couldn't find any eligible sections for that request. Check "
                "the course codes, or whether prerequisites/completed courses "
                "rule everything out.")

    # Headline
    if not result.relaxations and not result.dropped_courses:
        if c.no_gaps:
            lines.append(f"Here's an optimal, conflict-free schedule with all "
                         f"{n} course(s), packed as tightly as the offerings allow.")
        else:
            lines.append(f"Here's a conflict-free schedule with all {n} course(s).")
    else:
        lines.append("Your original constraints were over-tight, so I negotiated "
                     "the closest valid schedule I could:")

    # What had to give (the negotiation trace)
    if result.relaxations:
        lines.append("")
        lines.append("To make it fit, I:")
        for r in result.relaxations:
            lines.append(f"  • {r}")

    if result.dropped_courses:
        lines.append("")
        lines.append("I could not fit every course even after that, so I left out: "
                     f"**{', '.join(result.dropped_courses)}** "
                     "(those sections clash with the rest at every available slot). "
                     "Options: relax a constraint further, or swap one of these for "
                     "a different course.")

    if c.no_gaps and result.campus_minutes is not None:
        hrs = result.campus_minutes / 60
        lines.append("")
        lines.append(f"Total time on campus across the week: ~{hrs:.1f} hours "
                     "(minimised).")

    # Grounding note - this is the anti-hallucination guarantee, verified
    # against the constraints actually in force (post-relaxation).
    if report.get("correct"):
        lines.append("")
        lines.append("_Every section above is a real row in the course catalog, "
                     "with no time conflicts (verified)._")
    return "\n".join(lines)


def suggest_schedule(question: str, history: list[dict] = None,
                     constraints: _ConstraintModel = None,
                     reasoning_raw: str = None) -> dict:
    """Build a schedule for a request already known to be a scheduling one.

    ``constraints`` may be passed in if extraction already happened (in the
    router) to avoid a second LLM call.
    """
    if constraints is None:
        constraints, reasoning_raw = extract_constraints(question, history)

    c = ScheduleConstraints(
        desired_courses=[code.upper() for code in constraints.desired_courses],
        desired_count=constraints.desired_count,
        completed_courses=[code.upper() for code in constraints.completed_courses],
        earliest=constraints.earliest,
        latest=constraints.latest,
        allowed_days=constraints.allowed_days,
        max_per_day=constraints.max_per_day,
        no_gaps=constraints.no_gaps,
    )

    sections = fetch_eligible_sections(
        c.desired_courses if c.desired_courses else None,
        c.completed_courses,
    )
    result = solve_with_relaxation(sections, c)
    # Judge correctness against the constraints actually in force for the
    # returned schedule (the effective set after any negotiated relaxation).
    effective = result.effective or c
    criteria = (validate_schedule(result.chosen, effective, check_db=True)
                if result.chosen else {})
    explanation = _render_explanation(c, result, criteria)
    rows = _schedule_rows(result)

    return {
        "question": question,
        "reasoning": explanation,
        "sql": None,                     # this path uses a parameterised query, not LLM SQL
        "rows": rows if rows else None,
        "error": None if result.chosen else "No eligible sections for that request.",
        "raw_response": reasoning_raw or json.dumps({"constraints": constraints.model_dump()}),
        # richer fields (ignored by the strict API model, used by /schedule + tests)
        "mode": "schedule",
        "relaxations": result.relaxations,
        "dropped_courses": result.dropped_courses,
        "criteria": criteria,
    }


@mlflow.trace(name="schedule_agent.respond", span_type="AGENT")
def respond(question: str, history: list[dict] = None) -> dict:
    """Single entry point: route a message to the scheduler or the lookup agent.

    Extraction runs once. If it's a build-a-schedule request we solve it
    deterministically (no further LLM call); otherwise we defer to the existing
    ``ask()`` text-to-SQL lookup path, unchanged.
    """
    span = mlflow.get_current_active_span()
    constraints, raw = extract_constraints(question, history)

    is_schedule = constraints.is_schedule_request and (
        constraints.desired_courses or constraints.desired_count
    )
    if span:
        span.set_attributes({"mode": "schedule" if is_schedule else "lookup"})

    if is_schedule:
        return suggest_schedule(question, history, constraints, raw)

    # Not a scheduling request - fall back to the original lookup agent.
    result = ask(question, history)
    result["mode"] = "lookup"
    return result
