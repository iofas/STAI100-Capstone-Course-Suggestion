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
| Prompt Engineering | Design and iterate on system prompts; few-shot, chain-of-thought, structured prompt patterns | bea | In progress (see `sql_agent/prompts.py`) |
| Structured Outputs | Return typed, schema-validated responses (JSON, Pydantic, etc.) | jp | **Working** (see `sql_agent/agent.py`) |
| Disambiguation | Detect ambiguous inputs and clarify intent before proceeding | — | Not started |
| RAG | Retrieve relevant context from a vector/SQL/graph store to ground responses | bea | Not started |
| Memory | Short-term session memory and/or long-term persistent memory | jp | **Working** (see `demo_sql_agent.py` & `api.py`) |
| Guardrails | Input/output validation, topic filtering, safety checks | — | Not started |
| ReAct Agent | Reasoning + acting loop, iterative planning and execution | — | Not started |
| SQL Agent | Generate and execute SQL against a relational DB from natural language | bea | **Working** (see `sql_agent/`) |
| Tool Use | Integrate at least one external tool/API | — | Not started |
| Chat UI | Conversational interface (e.g. Streamlit, Gradio) | kean | Not started |
| API Endpoint | Expose the agent via a REST API | gideon | **Working** (see `api.py`) |
| LLMOps Monitoring | Log traces, latency, token usage, errors (e.g. MLflow) | gideon | **Working** (see `sql_agent/monitoring.py`) |
| Dockerization | Package the app in a Dockerfile with build/run docs | kean | Not started |
