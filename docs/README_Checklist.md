## Deliverables

- Presentation
- Technical write-up (business case, methodology, architecture, experiments, retrospective)
- Source code (this repo)

## Technical requirements

- Deployable, with LLMOps monitoring
- Containerized (Docker)
- Web UI
- API endpoint

## Module checklist

The project must implement 8 of the 13 modules below. Five are required
regardless of which 8 are chosen: **Prompt Engineering**, **Chat UI**,
**API Endpoint**, **LLMOps Monitoring**, **Dockerization**.

| Module | Description | Owner | Status |
|---|---|---|---|
| Prompt Engineering | System prompt grounded in the live SQLite schema, 7 few-shot examples showing the reasoning-then-SQL pattern (including a rejected DELETE example), strict two-key JSON output contract | bea | **Working** (see `sql_agent/prompts.py`) |
| Structured Outputs | Pydantic validates both the LLM's own JSON output (`AgentOutput`: reasoning + sql) and the API's request/response shapes (`AskRequest`/`AskResponse`) | jp | **Working** (see `sql_agent/agent.py`, `apps/api.py`) |
| Disambiguation | Detect ambiguous inputs and clarify intent before proceeding | — | Not started |
| RAG | Retrieve relevant context from a vector/SQL/graph store to ground responses | bea | Not started (schema grounding via `get_schema_description()` is used instead of retrieval) |
| Memory | Short-term, per-conversation memory only: `{role, content}` history list passed back and forth on every request; no persistent/long-term storage across sessions | jp | **Working** (see `examples/demo_sql_agent.py`, `apps/api.py`, `apps/chat_ui.py`) |
| Guardrails | Regex-based safety checks block INSERT/UPDATE/DELETE/DROP/etc., enforce a single SELECT statement, block comma joins, and restrict table references to `course_offerings`/`course_prerequisites`; SQLite connection also opened in read-only (`PRAGMA query_only`) as defense-in-depth | — | **Working** (see `sql_agent/agent.py` `_is_safe_select`, `sql_agent/db.py`) |
| ReAct Agent | Reasoning + acting loop, iterative planning and execution | — | Not started (single-shot generate-then-execute; DeepSeek never sees query results or retries, no tool-calling loop) |
| SQL Agent | Generate and execute SQL against a SQLite database from natural language | bea | **Working** (see `sql_agent/`) |
| Tool Use | Integrate at least one external tool/API | — | Not started (no function/tool calling - DeepSeek only returns JSON text, `sql_agent.ask()` executes SQL itself afterward) |
| Chat UI | Streamlit conversational interface that calls the API over HTTP (does not import `sql_agent` directly) | kean | **Working** (see `apps/chat_ui.py`) |
| API Endpoint | Expose the agent via a REST API | gideon | **Working** (see `apps/api.py`) |
| LLMOps Monitoring | MLflow tracing: `mlflow.openai.autolog()` captures every DeepSeek call's latency/tokens automatically, and a manual `@mlflow.trace` span around `ask()` adds guardrail-rejection/error/row-count attributes | gideon | **Working** (see `sql_agent/monitoring.py`) |
| Dockerization | Single `python:3.12-slim` image shared by `api`/`chatui` (different `command:` per service) plus a `mlflow` service, orchestrated via `docker-compose.yml` | kean | **Working** (see `Dockerfile`, `docker-compose.yml`) |
