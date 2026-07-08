"""
Core SQL Agent logic: natural language -> generated SQL -> safety-checked ->
executed against course_offerings.db -> structured result.
"""
import re
import json
import mlflow
from openai import OpenAI
from pydantic import BaseModel, ValidationError

from .config import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    DEEPSEEK_TIMEOUT_SECONDS,
    require_api_key,
)
from .db import PREREQUISITES_TABLE_NAME, TABLE_NAME, get_connection, get_schema_description
from .monitoring import setup_tracing
from .prompts import build_messages

setup_tracing()

_client = None

class AgentOutput(BaseModel):
    reasoning: str
    sql: str

def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=require_api_key(),
            base_url=DEEPSEEK_BASE_URL,
            timeout=DEEPSEEK_TIMEOUT_SECONDS,
        )
    return _client

_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|ATTACH|DETACH|PRAGMA|CREATE|REPLACE|VACUUM)\b",
    re.IGNORECASE,
)
_TABLE_REF_PATTERN = re.compile(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE)
# Old-style comma joins (FROM a, b) would let a table slip past _TABLE_REF_PATTERN
# uncaptured, since it only looks at the identifier right after FROM/JOIN.
_COMMA_JOIN_PATTERN = re.compile(r"\bFROM\s+[a-zA-Z_][a-zA-Z0-9_]*\s*,", re.IGNORECASE)
_ALLOWED_TABLES = {TABLE_NAME.lower(), PREREQUISITES_TABLE_NAME.lower()}

def _is_safe_select(sql: str) -> bool:
    if not sql or sql.upper() == "NONE": return False
    if _FORBIDDEN_KEYWORDS.search(sql): return False
    body = sql.strip().strip(";")
    if ";" in body or not body.upper().startswith("SELECT"): return False
    if _COMMA_JOIN_PATTERN.search(body): return False
    referenced_tables = {t.lower() for t in _TABLE_REF_PATTERN.findall(body)}
    if not referenced_tables: return False
    if not referenced_tables.issubset(_ALLOWED_TABLES): return False
    return True

def generate_sql(question: str, history: list[dict] = None) -> dict:
    schema = get_schema_description()
    messages = build_messages(schema, question, history)

    response = _get_client().chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=messages,
        temperature=0,
        response_format={"type": "json_object"}, # Forces JSON output
        # Disable DeepSeek's built-in extended-thinking mode: our prompt
        # already asks the model to show its reasoning explicitly (see
        # prompts.py), and thinking mode doesn't support temperature=0,
        # which we want here for deterministic SQL generation.
        extra_body={"thinking": {"type": "disabled"}},
    )
    
    raw = response.choices[0].message.content
    
    # Pydantic validation
    try:
        parsed_data = AgentOutput.model_validate_json(raw)
        reasoning = parsed_data.reasoning
        sql = parsed_data.sql.strip().strip("`").strip()
    except ValidationError as e:
        reasoning = "Failed to parse JSON response."
        sql = "NONE"

    return {"question": question, "reasoning": reasoning, "sql": sql, "raw_response": raw}

def run_query(sql: str) -> list[dict]:
    with get_connection() as conn:
        cur = conn.execute(sql)
        return [dict(row) for row in cur.fetchall()]

@mlflow.trace(name="sql_agent.ask", span_type="AGENT")
def ask(question: str, history: list[dict] = None) -> dict:
    """
    End-to-end entry point: question (+ optional history) -> generated SQL -> 
    guardrail check -> execution -> structured result. This is the function 
    other modules (Chat UI, API endpoint) should import and call.

    Wrapped in an MLflow trace so every call - including the nested LLM
    call captured by mlflow.openai.autolog() - is logged with latency,
    token usage, and (via the span attributes below) guardrail rejections,
    SQL execution errors, and row counts.
    """
    span = mlflow.get_current_active_span()
    
    result = generate_sql(question, history)
    sql = result["sql"]

    # Guardrail check: if the SQL is empty, "NONE", or unsafe, reject it
    if not sql or sql.upper() == "NONE":
        if span: 
            span.set_attributes({"row_count": 0, "status": "unanswerable"})
        return {**result, "rows": [], "error": None}

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