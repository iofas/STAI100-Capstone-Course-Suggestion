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
    """Return a lazily-created, process-wide OpenAI client pointed at DeepSeek.

    The client is built on first use (so importing this module doesn't require
    an API key) and cached in the module-level `_client` for reuse.

    Returns:
        A configured `openai.OpenAI` instance (DeepSeek base URL, API key from
        the environment, request timeout from config).
    """
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
    """Guardrail: decide whether a generated SQL string is safe to execute.

    A statement is considered safe only if it is a single, read-only SELECT
    that touches nothing but the whitelisted tables. Everything else (writes,
    schema changes, multiple statements, comma-joins that could smuggle in an
    unlisted table, or references to tables outside the whitelist) is rejected.

    Args:
        sql: The raw SQL string produced by the LLM. May be empty or the
            sentinel "NONE" when the model declined to answer.

    Returns:
        True if `sql` is a single read-only SELECT against only the allowed
        tables (see `_ALLOWED_TABLES`); False otherwise.
    """
    if not sql or sql.upper() == "NONE": return False
    if _FORBIDDEN_KEYWORDS.search(sql): return False
    body = sql.strip().strip(";")
    if ";" in body or not body.upper().startswith("SELECT"): return False
    if _COMMA_JOIN_PATTERN.search(body): return False
    referenced_tables = {t.lower() for t in _TABLE_REF_PATTERN.findall(body)}
    if not referenced_tables: return False
    if not referenced_tables.issubset(_ALLOWED_TABLES): return False
    return True

_JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(raw: str) -> str:
    """Pull the JSON object out of a model reply that wrapped it in extra text.

    `response_format={"type": "json_object"}` almost always yields bare JSON,
    but a reply that arrives wrapped in ```json fences or with a stray sentence
    around it would otherwise fail validation and be reported as an
    unanswerable question. Salvage the outermost {...} block when that happens.

    Args:
        raw: The model's reply content, or None if the reply had no content.

    Returns:
        The substring from the first "{" to the last "}", or `raw` unchanged
        (empty string if None) when no such block exists - in which case
        validation fails as before.
    """
    if not raw:
        return ""
    match = _JSON_OBJECT_PATTERN.search(raw)
    return match.group(0) if match else raw


def generate_sql(question: str, history: list[dict] = None) -> dict:
    """Ask the LLM to translate a natural-language question into SQL.

    Builds the prompt (system prompt + schema + few-shot examples + prior
    turns), calls DeepSeek with deterministic settings (temperature=0, forced
    JSON output), and validates the reply into a reasoning/SQL pair. This does
    NOT run any guardrail check or execute the query - see `ask()` for that.

    Args:
        question: The student's natural-language question, e.g.
            "What sections of GEARTAP are on Mondays?".
        history: Optional prior conversation as a list of
            ``{"role": "user"|"assistant", "content": str}`` messages, used to
            resolve follow-ups ("show me GEWORLD instead"). Pass None (the
            default) or an empty list for a fresh, single-turn question.

    Returns:
        A dict with keys:
            - ``question``: the original question, echoed back.
            - ``reasoning``: the model's stated reasoning for the SQL.
            - ``sql``: the generated SELECT, or the sentinel ``"NONE"`` when
              the model declined or the JSON reply failed validation.
            - ``raw_response``: the unparsed JSON string from the model, kept
              for debugging/tracing.
            - ``tokens``: prompt/completion/total token counts for the call
              (values are None if the provider didn't report usage).
    """
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

    # Token counts, for the cost/efficiency metrics in evaluation/eval_agent.py.
    # Absent on some providers, so never assume the field is there.
    usage = getattr(response, "usage", None)
    tokens = {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }

    # Pydantic validation
    try:
        parsed_data = AgentOutput.model_validate_json(_extract_json(raw))
        reasoning = parsed_data.reasoning
        sql = parsed_data.sql.strip().strip("`").strip()
    except ValidationError as e:
        # A parse failure and a genuine refusal both end up as sql == "NONE",
        # so keep the reason distinguishable in the trace - otherwise a
        # malformed reply looks exactly like "the model declined to answer".
        reasoning = f"Failed to parse JSON response: {e}"
        sql = "NONE"

    return {"question": question, "reasoning": reasoning, "sql": sql,
            "raw_response": raw, "tokens": tokens}

def run_query(sql: str) -> list[dict]:
    """Execute an already-safety-checked SELECT and return the rows.

    Callers are responsible for validating `sql` with `_is_safe_select()`
    first; this function runs whatever it is given.

    Args:
        sql: A read-only SELECT statement to run against course_offerings.db.

    Returns:
        The result set as a list of dicts (one per row, column name -> value).

    Raises:
        sqlite3.Error: If the SQL is invalid or execution otherwise fails; the
            caller (`ask()`) catches this and surfaces it in the result.
    """
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

    Args:
        question: The student's natural-language question.
        history: Optional prior conversation as a list of
            ``{"role", "content"}`` messages for follow-up context. Defaults
            to None (treated as a fresh, single-turn question).

    Returns:
        The dict from `generate_sql()` (``question``/``reasoning``/``sql``/
        ``raw_response``) plus two more keys describing the outcome:
            - ``rows``: the executed query's rows on success; ``[]`` when the
              question was unanswerable (sql == "NONE"); ``None`` when the SQL
              was rejected by the guardrail or failed to execute.
            - ``error``: None on success/unanswerable, or a human-readable
              message when the guardrail rejected the SQL or execution failed.
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