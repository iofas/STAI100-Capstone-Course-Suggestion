"""
Scheduling evaluation: deterministic ILP pipeline vs. an LLM-only baseline.

This is the head-to-head experiment behind the project's central claim: a
deterministic optimizer never produces an invalid schedule, while asking an LLM
to build the timetable directly (the "wrapper" approach) does.

Method - both systems get the *identical* input, so only the scheduling step
differs:
  * SAME structured constraints (we define them per scenario, so the LLM's
    constraint-extraction step is not what's being tested here);
  * SAME candidate sections (real rows from course_offerings.db, capped per
    course so the prompt is reasonable and both systems see one shared pool).

  System A (ours):  scheduler.solve_with_relaxation()  -> CP-SAT ILP.
  System B (base):  ask the LLM to pick a conflict-free set of sections.

Both outputs are scored by the SAME validator (scheduler.validate_schedule,
C1-C6) against the ORIGINAL constraints, with C3 grounding checked against the
real database. A section the LLM invents fails C3; a clash fails C1; a repeated
course fails C2; an out-of-scope course fails C4; a violated time/day/max limit
fails C5.

Run (from the repo root):
    python -m evaluation.eval_scheduler   # prints tables, writes evaluation/eval_results.md

Costs one DeepSeek call per scenario (the baseline). The ILP side is free.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

from sql_agent.agent import _get_client
from sql_agent.config import DEEPSEEK_MODEL
from sql_agent.scheduler import (
    ScheduleConstraints,
    Section,
    fetch_eligible_sections,
    minutes_to_hhmm,
    solve_with_relaxation,
    validate_schedule,
)

CANDIDATES_PER_COURSE = 12   # cap so both systems share a bounded, equal pool

# The scenarios below use archived offerings, so this eval defaults to the
# archived term (1241). Override with SCHEDULE_TERM once the new term is loaded.
EVAL_TERM = os.getenv("SCHEDULE_TERM", "1241")


# --------------------------------------------------------------------------- #
# Scenarios - real DLSU course codes, varied constraints, all explicit-course.
# --------------------------------------------------------------------------- #
SCENARIOS = [
    {"name": "3 courses, no extra constraints",
     "c": dict(desired_courses=["GEARTAP", "GEWORLD", "LCFAITH"])},
    {"name": "3 courses, 09:00-15:00 window",
     "c": dict(desired_courses=["GEARTAP", "GEWORLD", "GEETHIC"],
               earliest="09:00", latest="15:00")},
    {"name": "4 courses, compact + max 2/day",
     "c": dict(desired_courses=["GEARTAP", "GEWORLD", "LCFAITH", "GERIZAL"],
               no_gaps=True, max_per_day=2)},
    {"name": "3 courses, Mon/Wed/Fri only",
     "c": dict(desired_courses=["GEUSELF", "GESTSOC", "GERPHIS"],
               allowed_days=["M", "W", "F"])},
    {"name": "4 courses, nothing after 16:00",
     "c": dict(desired_courses=["GEARTAP", "GEETHIC", "GEWORLD", "LCENWRD"],
               latest="16:00")},
    {"name": "5 courses, compact",
     "c": dict(desired_courses=["GEARTAP", "GEWORLD", "LCFAITH", "GERIZAL", "GEUSELF"],
               no_gaps=True)},
    {"name": "2 courses, tight 10:00-13:00 window",
     "c": dict(desired_courses=["GEWORLD", "LCFAITH"],
               earliest="10:00", latest="13:00")},
    {"name": "prereq: LCLSTWO after LCLSONE",
     "c": dict(desired_courses=["LCLSTWO", "GEWORLD", "LCFAITH"],
               completed_courses=["LCLSONE"])},
    {"name": "4 courses, M-H, max 2/day, compact",
     "c": dict(desired_courses=["GEARTAP", "GEETHIC", "GERIZAL", "GESTSOC"],
               allowed_days=["M", "T", "W", "H"], max_per_day=2, no_gaps=True)},
    {"name": "6 courses, compact (hard)",
     "c": dict(desired_courses=["GEARTAP", "GEWORLD", "LCFAITH", "GERIZAL",
                                "GEUSELF", "GEETHIC"], no_gaps=True)},
]


def _cap_candidates(sections: list[Section], n: int) -> list[Section]:
    by_course: dict[str, list[Section]] = defaultdict(list)
    for s in sorted(sections, key=lambda s: (s.course_code, s.section)):
        by_course[s.course_code].append(s)
    out: list[Section] = []
    for lst in by_course.values():
        out.extend(lst[:n])
    return out


# --------------------------------------------------------------------------- #
# System B: the LLM-only baseline ("wrapper") - the model builds the timetable.
# --------------------------------------------------------------------------- #
_BASELINE_SYSTEM = """You are a course-scheduling assistant. You are given a list \
of available course SECTIONS (each with its weekly meeting days and times) and a \
set of constraints. Build a valid weekly schedule by CHOOSING sections from the \
list.

Requirements: choose exactly one section per requested course; no two chosen \
sections may overlap in time on the same day; respect every stated constraint. \
Only use sections that appear in the provided list.

Respond with ONLY JSON: {"schedule": [{"course_code": "...", "section": "..."}, \
...]}. No other text."""


def _format_candidates(sections: list[Section]) -> str:
    lines = []
    for s in sections:
        mt = " , ".join(
            f"{m.day} {minutes_to_hhmm(m.start)}-{minutes_to_hhmm(m.end)}"
            for m in s.meetings
        )
        lines.append(f"{s.course_code} {s.section}: {mt}")
    return "\n".join(lines)


def _constraints_text(c: ScheduleConstraints) -> str:
    parts = []
    if c.desired_courses:
        parts.append(f"Include exactly these courses: {', '.join(c.desired_courses)}.")
    if c.earliest:
        parts.append(f"No class may start before {c.earliest}.")
    if c.latest:
        parts.append(f"No class may end after {c.latest}.")
    if c.allowed_days:
        parts.append(f"Only these weekday codes are allowed: {', '.join(c.allowed_days)} "
                     "(M=Mon T=Tue W=Wed H=Thu F=Fri S=Sat).")
    if c.max_per_day:
        parts.append(f"At most {c.max_per_day} classes on any single day.")
    if c.no_gaps:
        parts.append("Prefer a compact schedule with minimal gaps between classes.")
    return " ".join(parts)


def llm_baseline_schedule(candidates: list[Section], c: ScheduleConstraints) -> list[Section]:
    """Ask the LLM to pick the schedule directly (no solver). Map its picks back
    to real Section objects; unmatched picks become placeholders that will fail
    the C3 grounding check."""
    lookup = {(s.course_code.upper(), str(s.section).upper()): s for s in candidates}
    user = (
        "Available sections:\n" + _format_candidates(candidates)
        + "\n\nConstraints: " + _constraints_text(c)
    )
    resp = _get_client().chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[{"role": "system", "content": _BASELINE_SYSTEM},
                  {"role": "user", "content": user}],
        temperature=0,
        response_format={"type": "json_object"},
        extra_body={"thinking": {"type": "disabled"}},
    )
    try:
        picks = json.loads(resp.choices[0].message.content).get("schedule", [])
    except (json.JSONDecodeError, AttributeError):
        return []

    chosen: list[Section] = []
    for p in picks:
        code = str(p.get("course_code", "")).upper()
        sect = str(p.get("section", "")).upper()
        match = lookup.get((code, sect))
        if match is not None:
            chosen.append(match)
        else:
            # The model named a section not in the provided pool - keep it so
            # the grounding check (C3) can flag it as hallucinated.
            chosen.append(Section(course_code=code, section=sect, meetings=[]))
    return chosen


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
_CRITERIA = ["C1_no_overlap", "C2_no_duplicates", "C3_grounded",
             "C4_scope_faithful", "C5_constraints_hold"]


def _is_complete(chosen: list[Section], c: ScheduleConstraints) -> bool:
    got = {s.course_code.upper() for s in chosen}
    if c.desired_count:
        return len(got) >= c.desired_count
    return {code.upper() for code in c.desired_courses}.issubset(got)


def _score(chosen: list[Section], c: ScheduleConstraints) -> dict:
    report = validate_schedule(chosen, c, check_db=True) if chosen else {
        k: False for k in _CRITERIA + ["correct"]
    }
    report["valid"] = all(report.get(k) for k in _CRITERIA[:4])   # C1-C4
    report["complete"] = _is_complete(chosen, c)
    return report


def main() -> None:
    client_ok = True
    rows = []
    for sc in SCENARIOS:
        c = ScheduleConstraints(**sc["c"])
        candidates = _cap_candidates(
            fetch_eligible_sections(c.desired_courses or None, c.completed_courses,
                                    term=EVAL_TERM),
            CANDIDATES_PER_COURSE,
        )
        ilp_res = solve_with_relaxation(candidates, c)
        ilp_score = _score(ilp_res.chosen, c)

        try:
            base_chosen = llm_baseline_schedule(candidates, c)
        except Exception as exc:  # noqa: BLE001
            client_ok = False
            print(f"  ! baseline LLM call failed: {exc}")
            base_chosen = []
        base_score = _score(base_chosen, c)

        rows.append({
            "name": sc["name"], "n_candidates": len(candidates),
            "ilp": ilp_score, "ilp_relaxed": bool(ilp_res.relaxations),
            "base": base_score,
        })
        print(f"[{sc['name']}]  candidates={len(candidates)}")
        print(f"   ILP : valid(C1-4)={ilp_score['valid']}  complete={ilp_score['complete']}"
              f"  C5={ilp_score['C5_constraints_hold']}  relaxed={bool(ilp_res.relaxations)}")
        print(f"   LLM : valid(C1-4)={base_score['valid']}  complete={base_score['complete']}"
              f"  C5={base_score['C5_constraints_hold']}  "
              f"[C1={base_score['C1_no_overlap']} C2={base_score['C2_no_duplicates']} "
              f"C3={base_score['C3_grounded']} C4={base_score['C4_scope_faithful']}]")

    _report(rows, client_ok)


def _pct(rows, system, key):
    n = len(rows)
    return 100.0 * sum(1 for r in rows if r[system].get(key)) / n if n else 0.0


def _report(rows, client_ok):
    n = len(rows)
    lines = ["# Scheduling Evaluation: ILP pipeline vs. LLM-only baseline", ""]
    if not client_ok:
        lines.append("> NOTE: at least one baseline LLM call failed; baseline "
                     "numbers are incomplete.\n")
    lines.append(f"Scenarios: **{n}**. Both systems given identical constraints "
                 f"and the same capped candidate pool (<={CANDIDATES_PER_COURSE}"
                 " sections/course). Scored by the C1-C6 validator against the "
                 "original constraints (C3 grounding checked against the DB).\n")

    header = ("| Metric | ILP (ours) | LLM-only baseline |\n"
              "|---|---|---|")
    metric_rows = [
        ("C1 no overlapping timeslots", "C1_no_overlap"),
        ("C2 no duplicate courses", "C2_no_duplicates"),
        ("C3 grounded (real sections)", "C3_grounded"),
        ("C4 only requested courses", "C4_scope_faithful"),
        ("C5 hard constraints hold", "C5_constraints_hold"),
        ("**Valid schedule (C1-C4)**", "valid"),
        ("All requested courses scheduled", "complete"),
    ]
    lines.append(header)
    for label, key in metric_rows:
        lines.append(f"| {label} | {_pct(rows, 'ilp', key):.0f}% | "
                     f"{_pct(rows, 'base', key):.0f}% |")

    relaxed = sum(1 for r in rows if r["ilp_relaxed"])
    lines.append("")
    lines.append(f"- ILP schedules that needed a **transparent relaxation** "
                 f"(reported to the student, not silent): {relaxed}/{n}.")
    lines.append("- The ILP is expected to score **100% on C1-C4 by "
                 "construction**: the validator's constraints are the solver's "
                 "constraints. Any baseline shortfall on C1/C2/C3 is a schedule "
                 "that looks plausible but is wrong - the exact failure mode the "
                 "project prevents.")
    lines.append("")
    lines.append("## Per-scenario")
    lines.append("| Scenario | ILP valid | ILP complete | LLM valid | LLM complete |")
    lines.append("|---|---|---|---|---|")
    for r in rows:
        lines.append(f"| {r['name']} | {'Y' if r['ilp']['valid'] else 'N'} | "
                     f"{'Y' if r['ilp']['complete'] else 'N'} | "
                     f"{'Y' if r['base']['valid'] else 'N'} | "
                     f"{'Y' if r['base']['complete'] else 'N'} |")

    text = "\n".join(lines)
    # Write next to this script (evaluation/eval_results.md) so the output
    # lands in the same folder regardless of the current working directory.
    out_path = Path(__file__).resolve().parent / "eval_results.md"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text + "\n")

    print("\n" + "=" * 70)
    print(text)
    print("=" * 70)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
