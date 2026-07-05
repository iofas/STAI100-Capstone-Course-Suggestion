"""
Core SQL Agent logic: natural language -> generated SQL -> safety-checked ->
executed against course_offerings.db -> structured result.
"""
import re

import mlflow
from openai import OpenAI

from .config import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    DEEPSEEK_TIMEOUT_SECONDS,
    require_api_key,
)
from .db import TABLE_NAME, get_connection, get_schema_description
from .monitoring import setup_tracing
from .prompts import build_messages

setup_tracing()

_client = None

# Guardrails: block any statement-altering keywords outright, even if they
# show up disguised inside a supposedly read-only query.
_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|ATTACH|DETACH|PRAGMA|CREATE|REPLACE|VACUUM)\b",
    re.IGNORECASE,
)
_RESPONSE_PATTERN = re.compile(r"REASONING:\s*(.*?)\s*SQL:\s*(.*)", re.DOTALL)


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=require_api_key(),
            base_url=DEEPSEEK_BASE_URL,
            timeout=DEEPSEEK_TIMEOUT_SECONDS,
        )
    return _client


def _parse_response(raw: str) -> tuple[str, str]:
    """Split the model's REASONING/SQL formatted reply into its two parts."""
    match = _RESPONSE_PATTERN.search(raw or "")
    if not match:
        return raw.strip() if raw else "", ""
    reasoning, sql = match.group(1).strip(), match.group(2).strip()
    sql = sql.strip().strip("`").strip()
    return reasoning, sql


def _is_safe_select(sql: str) -> bool:
    """Guardrail: only allow a single read-only SELECT against course_offerings."""
    if not sql or sql.upper() == "NONE":
        return False
    if _FORBIDDEN_KEYWORDS.search(sql):
        return False
    if not sql.lstrip().upper().startswith("SELECT"):
        return False
    # allow at most one trailing semicolon, i.e. no stacked statements
    body = sql.strip()
    if body.endswith(";"):
        body = body[:-1]
    if ";" in body:
        return False
    if TABLE_NAME not in sql:
        return False
    return True


def generate_sql(question: str) -> dict:
    """Ask the LLM to translate a question into SQL. Does not execute it."""
    schema = get_schema_description()
    messages = build_messages(schema, question)

    response = _get_client().chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=messages,
        temperature=0,
        # Disable DeepSeek's built-in extended-thinking mode: our prompt
        # already asks the model to show its reasoning explicitly (see
        # prompts.py), and thinking mode doesn't support temperature=0,
        # which we want here for deterministic SQL generation.
        extra_body={"thinking": {"type": "disabled"}},
    )
    raw = response.choices[0].message.content
    reasoning, sql = _parse_response(raw)
    return {"question": question, "reasoning": reasoning, "sql": sql, "raw_response": raw}


def run_query(sql: str) -> list[dict]:
    with get_connection() as conn:
        cur = conn.execute(sql)
        rows = cur.fetchall()
        return [dict(row) for row in rows]


@mlflow.trace(name="sql_agent.ask", span_type="AGENT")
def ask(question: str) -> dict:
    """
    End-to-end entry point: question -> generated SQL -> guardrail check ->
    execution -> structured result. This is the function other modules
    (Chat UI, API endpoint) should import and call.

    Wrapped in an MLflow trace so every call - including the nested LLM
    call captured by mlflow.openai.autolog() - is logged with latency,
    token usage, and (via the span attributes below) guardrail rejections,
    SQL execution errors, and row counts.
    """
    span = mlflow.get_current_active_span()

    result = generate_sql(question)
    sql = result["sql"]

    if not _is_safe_select(sql):
        error = (
            "Generated query was rejected by the safety guardrail (must be "
            f"a single read-only SELECT against {TABLE_NAME})."
        )
        if span:
            span.set_attributes({"guardrail_rejected": True, "error": error})
        return {**result, "rows": None, "error": error}

    try:
        rows = run_query(sql)
    except Exception as exc:  # surface DB errors as part of the structured result
        error = f"SQL execution failed: {exc}"
        if span:
            span.set_attributes({"sql_execution_error": True, "error": error})
        return {**result, "rows": None, "error": error}

    if span:
        span.set_attributes({"row_count": len(rows)})
    return {**result, "rows": rows, "error": None}
