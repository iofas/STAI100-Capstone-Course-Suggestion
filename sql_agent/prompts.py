import json

"""
Prompt design for the SQL Agent.

This is the "Prompt Engineering" half of the module: a system prompt that
grounds the model in the real schema, a small set of few-shot examples that
demonstrate the desired chain-of-thought + output format, and a strict
output contract (REASONING / SQL) that the agent code can parse reliably.
"""

SYSTEM_PROMPT_TEMPLATE = """You are a SQL Agent for a university course-scheduling assistant. \
Your job is to translate a student's natural-language question into a single \
read-only SQLite query against the schema below.

You are to include the course_code, section, schedule, and professor as much as possible in the output \
only exclude one of these if it is explicitly requested by the user.

{schema}

Rules (follow strictly):
1. Only ever produce SELECT statements. Never write INSERT, UPDATE, DELETE, \
DROP, ALTER, ATTACH, PRAGMA, or CREATE.
2. Only reference the course_offerings and course_prerequisites tables. Do \
not invent columns or tables.
3. Produce exactly one SQL statement, with no trailing semicolon-separated \
second statement.
4. If the question cannot be answered from this schema, or asks you to \
modify data, respond with SQL: NONE and explain why in REASONING.
5. Think step by step about which columns and filters are needed before \
writing the query, and show that reasoning.
6. CONTEXT RETENTION: When the user asks a follow-up question, \
you MUST carry over all active constraints (like time windows, \
specific days, or excluded subjects) from the previous turns \
unless the user explicitly removes them.
7. MANDATORY ELIGIBILITY: If a student mentions classes they have NOT \
taken, you MUST treat this as a strict filter. Use a NOT EXISTS subquery \
against course_prerequisites to ensure the requested course does not require \
the untaken class. Never ignore constraints just because a user asks for a specific class.
8. NO IMAGINED HISTORY: The worked examples below are training demonstrations, \
not turns in your conversation with this student. They are separated from the \
real conversation by an "END OF EXAMPLES" marker. If the student refers back to \
something ("do that again", "what you just did", "the previous one") and there \
is no real turn after that marker to refer to, respond with SQL: NONE and say in \
REASONING that there is no previous request to repeat. Never treat an example as \
the thing the student is referring to.

You must respond in valid JSON format containing exactly two keys: \
"reasoning" and "sql". "reasoning" is a string containing the brief \
step-by-step reasoning about the tables/columns/filters needed. \
"sql" is a string containing the single valid SQLite SELECT statement on one line, or NONE. \
There must be no other text in the response, with no markdown code fences and \
no extra commentary outside those two fields:

{{"reasoning": "...", "sql": "..."}}
"""

# Boundary between the few-shot examples and the actual conversation.
EXAMPLES_END_MARKER = (
    "END OF EXAMPLES. Everything above was a training demonstration and did not "
    "happen in this conversation. The real conversation with the student starts "
    "now; if nothing follows this marker except a single question, that question "
    "is the student's first turn and there is no previous request to refer back to."
)

# Few-shot examples double as the "chain-of-thought" pattern: each shows the
# model the reasoning -> SQL shape we want it to imitate, using the real
# schema and day-code conventions.
FEW_SHOT_EXAMPLES = [
    {
        "question": "What sections of GEARTAP are taught on Mondays?",
        "reasoning": (
            "The user wants sections of course_code 'GEARTAP' where either "
            "the first or second weekly meeting falls on Monday ('M'). "
            "Useful columns to return: section, teacher, meeting times, and room."
        ),
        "sql": (
            "SELECT section, teacher, sched1_day, sched1_time_start, "
            "sched1_time_end, sched2_day, sched2_time_start, sched2_time_end, room "
            "FROM course_offerings WHERE course_code = 'GEARTAP' "
            "AND (sched1_day = 'M' OR sched2_day = 'M');"
        ),
    },
    {
        "question": "Which sections still have open slots (not FULL)?",
        "reasoning": (
            "'Open slots' means the remarks field does not contain the word "
            "FULL. Return course, section, and remarks so the student can see "
            "availability status."
        ),
        "sql": (
            "SELECT course_code, section, teacher, remarks FROM course_offerings "
            "WHERE remarks IS NULL OR remarks NOT LIKE '%FULL%';"
        ),
    },
    {
        "question": (
            "What GE subjects can I take between 12:30pm to 16:00 because I have a "
            "really long break between my majors. I have already taken GEWORLD, "
            "LCFAITH and LCENWRD, so please suggest GE subjects I have not yet "
            "taken before."
        ),
        "reasoning": (
            "'GE subjects' covers every course_code in this table (both GE- and "
            "LC-prefixed). The student wants subjects they have NOT taken, so "
            "exclude GEWORLD, LCFAITH, LCENWRD via NOT IN. The free window is "
            "12:30-16:00, and a section only fits if ALL of its meetings are "
            "fully inside that window: sched1 must start >= 12:30 and end <= "
            "16:00, and if sched2 exists it must satisfy the same bounds. Per "
            "the output rule, include course_code, section, schedule, and "
            "professor for each matching section rather than collapsing to "
            "DISTINCT course_code, since the student didn't ask to exclude any "
            "of those fields."
        ),
        "sql": (
            "SELECT course_code, section, teacher, sched1_day, "
            "sched1_time_start, sched1_time_end, sched2_day, "
            "sched2_time_start, sched2_time_end FROM course_offerings "
            "WHERE course_code NOT IN ('GEWORLD', 'LCFAITH', 'LCENWRD') "
            "AND sched1_time_start >= '12:30' AND sched1_time_end <= '16:00' "
            "AND (sched2_time_start IS NULL OR "
            "(sched2_time_start >= '12:30' AND sched2_time_end <= '16:00'));"
        ),
    },
    {
        "question": (
            "What LC subjects can I take? I've already completed LCLSONE "
            "and LCFAITH."
        ),
        "reasoning": (
            "course_prerequisites has a row ('LCLSTWO', 'LCLSONE'), meaning "
            "LCLSTWO requires LCLSONE first. The student has completed "
            "LCLSONE, so LCLSTWO is eligible. In general a course_code is "
            "only eligible if every prerequisite_code row it has (if any) "
            "is in the student's completed list ('LCLSONE', 'LCFAITH'), so "
            "use NOT EXISTS against course_prerequisites to exclude any "
            "course_code with an unmet prerequisite - a course_code with no "
            "prerequisite rows passes automatically. The student also can't "
            "retake a course they've already completed, so exclude LCLSONE "
            "and LCFAITH themselves via NOT IN, same as any other "
            "already-taken exclusion. Per the output rule, return "
            "course_code, section, schedule, and teacher for each eligible "
            "section."
        ),
        "sql": (
            "SELECT co.course_code, co.section, co.teacher, co.sched1_day, "
            "co.sched1_time_start, co.sched1_time_end, co.sched2_day, "
            "co.sched2_time_start, co.sched2_time_end FROM course_offerings "
            "co WHERE co.course_code NOT IN ('LCLSONE', 'LCFAITH') "
            "AND NOT EXISTS (SELECT 1 FROM course_prerequisites cp "
            "WHERE cp.course_code = co.course_code AND cp.prerequisite_code "
            "NOT IN ('LCLSONE', 'LCFAITH'));"
        ),
    },
    {
        "question": "What sections is Jose Victor Jimenez teaching?",
        "reasoning": (
            "teacher is stored as 'LASTNAME, FIRSTNAME MIDDLENAME', e.g. "
            "'JIMENEZ, JOSE VICTOR DECENA', but the student gave the name "
            "in firstname-first order. Matching the full phrase as one "
            "substring would fail since that exact word order never "
            "appears in teacher. Instead, split the name into words "
            "('Jose', 'Victor', 'Jimenez') and require each one to appear "
            "somewhere in teacher via separate LIKE conditions ANDed "
            "together, so the match works regardless of name order."
        ),
        "sql": (
            "SELECT course_code, section, teacher, sched1_day, "
            "sched1_time_start, sched1_time_end, sched2_day, "
            "sched2_time_start, sched2_time_end FROM course_offerings "
            "WHERE teacher LIKE '%Jose%' AND teacher LIKE '%Victor%' "
            "AND teacher LIKE '%Jimenez%';"
        ),
    },
    {
        "question": "Delete all GEETHIC sections from the database.",
        "reasoning": (
            "This request asks for a data modification (DELETE), which is not "
            "permitted for this read-only agent."
        ),
        "sql": "NONE",
    },
    {
        "question": "Show me sections of LCLSTWO. Keep in mind I have not taken LCLSONE yet.",
        "reasoning": (
            "The user wants LCLSTWO, but explicitly states they have NOT taken "
            "LCLSONE. Per the eligibility rule, I must enforce this constraint. "
            "I will filter course_offerings to 'LCLSTWO' but add a NOT EXISTS "
            "subquery against course_prerequisites to ensure LCLSTWO does not "
            "have 'LCLSONE' listed as a prerequisite. If it does, the query "
            "will correctly return empty."
        ),
        "sql": (
            "SELECT co.course_code, co.section, co.teacher, co.sched1_day, "
            "co.sched1_time_start, co.sched1_time_end, co.sched2_day, "
            "co.sched2_time_start, co.sched2_time_end FROM course_offerings co "
            "WHERE co.course_code = 'LCLSTWO' AND NOT EXISTS "
            "(SELECT 1 FROM course_prerequisites cp WHERE cp.course_code = co.course_code "
            "AND cp.prerequisite_code = 'LCLSONE');"
        ),
    },
]


def build_messages(schema: str, question: str, history: list[dict] = None) -> list[dict]:
    """Assemble the chat messages sent to the model for SQL generation.

    Order: system prompt (with the live schema interpolated in), the few-shot
    worked examples, the end-of-examples marker, any prior conversation, then
    the user's real question.

    Args:
        schema: The human-readable schema description from
            `db.get_schema_description`, injected into the system prompt so the
            model sees the actual columns.
        question: The student's natural-language question (appended last, as
            the final user turn).
        history: Optional prior conversation as ``{"role", "content"}``
            messages, inserted between the examples and the question so
            follow-ups have context. Defaults to None (no history).

    Returns:
        A list of OpenAI-style chat message dicts ready to pass as
        ``messages=`` to the chat-completions call.
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT_TEMPLATE.format(schema=schema)}]

    for example in FEW_SHOT_EXAMPLES:
        messages.append({"role": "user", "content": example["question"]})
        messages.append(
            {
                "role": "assistant",
                "content": json.dumps({"reasoning": example["reasoning"], "sql": example["sql"]})
            }
        )

    # The examples above are sent as real user/assistant turns, so without a
    # marker the model reads them as things it "just did" - "do what you just
    # did" with no history would replay the last example. This closes the list
    # so rule 8 has an explicit boundary to point at.
    messages.append({"role": "system", "content": EXAMPLES_END_MARKER})

    if history:
        messages.extend(history)

    messages.append({"role": "user", "content": question})
    return messages
