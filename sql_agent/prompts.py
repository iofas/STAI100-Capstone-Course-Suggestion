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
2. Only reference the course_offerings table. Do not invent columns or tables.
3. Produce exactly one SQL statement, with no trailing semicolon-separated \
second statement.
4. If the question cannot be answered from this schema, or asks you to \
modify data, respond with SQL: NONE and explain why in REASONING.
5. Think step by step about which columns and filters are needed before \
writing the query, and show that reasoning.

You must respond in valid JSON format containing exactly two keys: \
"reasoning" and "sql". "reasoning" is a string containing the brief \
step-by-step reasoning about the tables/columns/filters needed. \
"sql" is a string containing the single valid SQLite SELECT statement on one line, or NONE. \
There must be no other text in the response, with no markdown code fences and \
no extra commentary outside those two fields:

{{"reasoning": "...", "sql": "..."}}
"""

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
        "question": "Delete all GEETHIC sections from the database.",
        "reasoning": (
            "This request asks for a data modification (DELETE), which is not "
            "permitted for this read-only agent."
        ),
        "sql": "NONE",
    },
]


def build_messages(schema: str, question: str, history: list[dict] = None) -> list[dict]:
    """Assemble the chat messages sent to the model: system prompt with the
    live schema, a few worked examples, then the user's real question."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT_TEMPLATE.format(schema=schema)}]

    for example in FEW_SHOT_EXAMPLES:
        messages.append({"role": "user", "content": example["question"]})
        messages.append(
            {
                "role": "assistant",
                "content": json.dumps({"reasoning": example["reasoning"], "sql": example["sql"]})
            }
        )

    if history:
        messages.extend(history)
    
    messages.append({"role": "user", "content": question})
    return messages
