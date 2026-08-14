# STAI100 Capstone: Course Suggestion Agent

An LLM-powered assistant that helps students pick General Education / elective
subjects (course code, section, schedule, room, teacher) based on natural
language requests, backed by a real course-offering dataset for the term.

It does two things: **look things up** (natural language → SQL → matching
sections) and **build a full timetable** (natural language → structured
constraints → a deterministic ILP solver → a verified, conflict-free schedule).
The language model only ever *translates*; the schedule itself is produced by an
optimizer that cannot hallucinate a clash or invent a section.



### Setup

1. **Download the database.** Grab the prebuilt `course_offerings.db` from the
[dataset](#dataset) Google Drive link and place it in the **repo root** (next to
`api.py`). This single file already contains both terms and the prerequisite
data — no separate load/build step needed.

2. Setup python requirements and environment
```bash
pip install -r requirements.txt
cp .env.example .env   # then edit .env and add your DEEPSEEK_API_KEY
```
3. **(Optional)** Install SQLite if you want to inspect the DB directly:
```powershell
winget install SQLite.SQLite
```
Then verify the data (should return `1241|644` and `1261|783`, i.e. 1427 total):
```powershell
sqlite3 course_offerings.db "SELECT term, COUNT(*) FROM course_offerings GROUP BY term;"
```
4. Start the MLflow tracking server (for LLMOps monitoring - latency, token
usage, and traces of every question). Leave this running in its own terminal:
```bash
uvx mlflow server
```

> The agent and scheduler read one term at a time, set by `SCHEDULE_TERM` in
> `.env` (default `1261`). Point it at `1241` to run against the archived term.
> See [scraper/README.md](scraper/README.md) to regenerate the current term's
> data from ArchersHub.

### Run

```bash
python -m examples.demo_sql_agent "What GE subjects can I take between 12:30pm and 16:00 that I haven't taken, given I've taken GEWORLD LCFAITH LCENWRD?"

# or interactively (with short-term conversation memory):
python -m examples.demo_sql_agent
```

Then open http://localhost:5000 to see the `sql_agent` experiment: every
call to `ask()` is logged as a trace with the question, model reasoning,
generated SQL, row count, latency, token usage, and any guardrail/execution
error. If the MLflow server isn't running, the agent still works - traces
just silently fail to upload (see `sql_agent/monitoring.py`).

### API

A REST wrapper around the same `ask()` function, for the Chat UI or any
other client to call over HTTP instead of importing the Python package:

```bash
uvicorn apps.api:app --reload
```

Interactive docs (try it out in the browser) are then at
http://localhost:8000/docs. Or call it directly:

```bash
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{
        "question": "What sections of GEARTAP are on Mondays?", 
        "history": []
      }'
```

`GET /health` is a plain liveness check. `POST /ask` returns
`{question, reasoning, sql, rows, error, updated_history, mode, relaxations,
dropped_courses, criteria}`. `mode` is `"lookup"` or `"schedule"`; for schedule
requests `rows` is the timetable, `relaxations`/`dropped_courses` record any
negotiation, and `criteria` is the C1-C7 correctness check plus the
complete/transparent/outcome grade. Guardrail rejections
and SQL errors still come back as a normal 200 with `error` populated; only a
genuine upstream failure (DeepSeek unreachable/timed out) returns a 502.

### Chat UI

A Streamlit front-end that talks to the API above over HTTP (it does not
import `sql_agent` directly). Start the API first, then in a separate
terminal:

```bash
streamlit run apps/chat_ui.py
```

Open http://localhost:8501. The sidebar shows whether it can reach the API
and lets you clear the conversation. By default it talks to
`http://localhost:8000`; point it elsewhere by setting `API_URL` before
running it, e.g. `API_URL=http://some-host:8000 streamlit run apps/chat_ui.py`.

## Docker

Everything above (MLflow, API, Chat UI) can also be run together with
Docker Compose instead of three separate terminals:

```bash
cp .env.example .env   # then edit .env and add your DEEPSEEK_API_KEY
docker compose up --build
```

This starts three containers, all built from the same image
(`python:3.12-slim`), differing only in which command they run:

| Service  | Container            | Port   | Runs                                          |
|----------|-----------------------|--------|-----------------------------------------------|
| `mlflow` | `course-agent-mlflow` | `5000` | MLflow tracking server                        |
| `api`    | `course-agent-api`    | `8000` | `uvicorn apps.api:app`                             |
| `chatui` | `course-agent-chatui` | `8501` | `streamlit run apps/chat_ui.py`                    |

Then open http://localhost:8501 for the Chat UI, http://localhost:8000/docs
for the API, and http://localhost:5000 for MLflow traces - same as running
everything locally, just containerized.

A few things worth knowing:
- `course_offerings.db` must already exist (downloaded via the Setup steps above)
  before running `docker compose up` - it's mounted read-only into the `api`
  container rather than baked into the image, since it's data, not code.
- The `api` container's `MLFLOW_TRACKING_URI` is overridden to
  `http://mlflow:5000` (the `mlflow` service's Docker network name), not
  `localhost`, since each container has its own `localhost`.
- To stop everything: `docker compose down` (add `-v` to also drop the
  `mlflow_data` volume, which wipes MLflow's stored traces/experiments).
- To rebuild after changing code or dependencies: `docker compose up --build`
  again.

## Repo structure

The repo is grouped by function: the two importable packages (`sql_agent/`,
`scraper/`) hold the core logic, and the runnable code is sorted into folders by
role (`apps/`, `examples/`, `evaluation/`, `tests/`).

```
course_offerings.db                SQLite DB, downloaded from Drive (1427 sections across terms 1241 + 1261)

sql_agent/                         core package: the agent, scheduler, and their prompts
  config.py                       env-based settings (DeepSeek API key, model, DB path, MLflow)
  db.py                           DB connection + schema introspection for prompting
  prompts.py                      system prompt, schema notes, few-shot examples (lookup)
  monitoring.py                   MLflow tracing setup (latency, token usage, traces)
  agent.py                        ask(question) -> generated SQL -> guardrails -> execution
  scheduler.py                    deterministic ILP schedule solver (CP-SAT) + relaxation + validator
  schedule_prompts.py             prompt: NL -> structured scheduling constraints (JSON)
  schedule_agent.py               respond(): routes lookup vs schedule, runs the negotiation loop

scraper/                           ArchersHub scraper: pull current-term offerings -> DB (see scraper/README.md)

apps/                              user-facing entry points
  api.py                          REST API (FastAPI); /ask routes lookup vs schedule
  chat_ui.py                      Streamlit Chat UI (no form), calls the API over HTTP

examples/
  demo_sql_agent.py               CLI to try the SQL Agent (python -m examples.demo_sql_agent)

evaluation/
  eval_cases.py                   golden dataset: reference SQL + adversarial probes
                                  (python -m evaluation.eval_cases prints dataset quality)
  eval_agent.py                   SQL agent eval: execution accuracy, term scoping,
                                  determinism, safety, latency/tokens
  eval_agent_results.md           the agent eval's recorded output
  eval_versions.py                runs the same eval against an older checkout of
                                  sql_agent/, so v1/v2/v3 share one metric
  eval_scheduler.py               deterministic ILP vs. LLM-only baseline experiment
  eval_results.md                 the scheduling experiment's recorded output

tests/
  test_cases.py                   39 predefined test cases across 8 categories
  test_agent.py                   pytest runner evaluating the SQL agent (live LLM)
  test_scheduler.py               offline pytest for the ILP solver + relaxation + C1-C7 (no LLM/DB)
  test_scheduler_db.py            C3 grounding + C6 eligibility against the real catalog (no LLM)

docs/
  README_Checklist.md             deliverables checklist / module ownership

pyproject.toml                     project metadata + pytest config (adds repo root to sys.path)
requirements.txt
.env.example                       copy to .env and fill in your DeepSeek API key
Dockerfile                         single image shared by the api/chatui containers
docker-compose.yml                 runs mlflow + api + chatui together
```

## Schedule Agent (data-science layer)

The scheduling capability is the anti-hallucination core of the project. When a
student asks the agent to **build a timetable** ("build me a compact schedule of
GEARTAP, GEWORLD and LCFAITH before 3pm, max 2 classes a day"), the request is
routed away from free-text generation and into a deterministic pipeline:

1. **Translate (LLM).** `schedule_agent.extract_constraints()` turns the message
   into a structured `ScheduleConstraints` object (desired courses or a count,
   completed courses, earliest/latest time, allowed days, max classes/day, a
   no-gaps preference). This is the *only* step the model does.
2. **Retrieve (SQL).** `scheduler.fetch_eligible_sections()` pulls candidate
   sections with a parameterised, read-only query - excluding already-completed
   courses and any course with an unmet prerequisite.
3. **Schedule (ILP).** `scheduler.solve_with_relaxation()` builds an Integer
   Linear Program and solves it with Google OR-Tools' **CP-SAT** engine: choose
   one section per course, **no time overlaps**, respect max classes/day and
   allowed days, and (as a soft objective) **minimise time on campus / gaps**.

**Negotiation on over-constrained requests.** If no schedule satisfies every
constraint, the solver doesn't fail or fabricate one - it loosens the
lowest-priority constraint (raise max/day → lift day limits → widen the time
window → as a last resort, drop a course), re-solves, and **reports exactly what
it relaxed**, so the student can decide. This multi-turn "diagnose → relax →
explain" behaviour is what makes it an agent rather than an LLM wrapper.

**Verified, not trusted.** Every returned schedule is graded by
`scheduler.grade_schedule()` on three separate axes - **correct** (nothing in it
is wrong), **complete** (the student got everything they asked for), and
**transparent** (every compromise is reported). Correctness is the seven hard
criteria C1-C7: no overlapping timeslots, no duplicates, every section *and its
times* match the catalog, only requested courses appear, the effective hard
constraints hold, every course is one the student is eligible for, and every
section is actually attendable. The API returns these as `criteria`, and the
Chat UI shows a ✓ Verified badge.

Correct and complete are deliberately different questions: an over-constrained
request has no complete answer, and the honest response is a correct schedule
that says what could not be fitted - which is why a compromise that goes
*unreported* is graded as a failure even when every section in the schedule is
fine. The criteria are defined in full at the validator section of
`sql_agent/scheduler.py`.

There is **no input form** - students describe what they want in the chat, and
the sidebar lists the filters they can ask for in plain language.

Try it from the CLI, the API (`POST /ask`), or the Chat UI:

```bash
python -c "from sql_agent import respond; import json; print(json.dumps(respond('Build me a compact schedule with GEARTAP, GEWORLD and LCFAITH before 3pm, max 2 classes a day')['reasoning']))"
```

Run the offline scheduler tests (no API key or DeepSeek call needed):

```bash
pytest tests/test_scheduler.py tests/test_scheduler_db.py -v
```

## Evaluation

The pytest suites are the regression gate; `evaluation/` is the measuring
instrument. Three commands, in increasing order of what they cost to run:

```bash
python -m evaluation.eval_cases
```

Golden-dataset quality - how many test cases can actually distinguish a right
answer from a wrong one. No API calls.

```bash
python -m evaluation.eval_scheduler
```

The ILP pipeline vs. an LLM-only baseline on identical scenarios, scored by the
same C1-C7 validator. One DeepSeek call per scenario.

```bash
python -m evaluation.eval_agent --runs 1
```

The SQL agent scored on **execution accuracy** (does the query return the same
rows as a reference query?) rather than substring matching, plus term-scoping
compliance, structural safety via a real SQL parser, adversarial refusal rate,
and latency/token cost. Costs (cases x runs) calls - 51 per run. Raise `--runs`
to measure run-to-run determinism; that multiplies the cost accordingly.

## Dataset

`course_offerings` holds DLSU course offerings across two terms, tagged by a
`term` column: **`1241`** (644 archived sections) and **`1261`** (783 current
sections, scraped from ArchersHub). Each row is one section: course code,
teacher, section, up to two weekly meeting times (day + start/end), room,
and remarks (e.g. FULL, HYBRID, ONLINE). Students refer to every course code
here as a "GE subject" regardless of whether it's GE- or LC-prefixed — both
are the same general-elective pool. `course_prerequisites` records which
courses require another course first (curriculum-wide, not per-term).

The prebuilt database (both terms + prerequisites) may be downloaded
[from this Google Drive](https://drive.google.com/file/d/1ZzfVUj8Z1i4LvZEnIToUQ4ZYxrl0pWsJ/view?usp=sharing)
which is accessible to those with a valid DLSU email.
It is not publicly available since it contains personally identifiable information such as the professor's names.

## SQL Agent

Translates a natural-language question into a single read-only SQL query
against `course_offerings`, executes it, and returns a structured result.
Uses DeepSeek's API via the `openai` SDK, enforcing strict JSON outputs via 
Pydantic validation, and maintains short-term conversational memory. It includes a 
guardrail that only allows single `SELECT` statements against the `course_offerings` table.

Other modules (Chat UI, API endpoint) should call `sql_agent.ask(question)`,
which returns `{question, reasoning, sql, raw_response, rows, error}`.

## Test Agent

A evaluation suite using `pytest` to verify the LLM's accuracy, context retention,
and security guardrails. It runs the agent against 30+ predefined scenarios across 8 categories
- Basic Lookups (e.g., "What sections of GEARTAP are taught on Mondays?")
- Time Window Constraints (e.g., "Between 12:00 and 15:00")
- Prerequisite/Exclusion Logic (e.g., "Haven't taken X")
- Professor Lookups (e.g., "Taught by Sir Villacorta")
- Status Filters (e.g., "Sections that are not Hybrid")
- Guardrail Checks (e.g., "Drop tables", "Insert fake class")
- Edge Cases (e.g., "What is the meaning of life?", "What sections of GEARTAP are taught on Mondays and Tuesdays?")
- Memory (e.g., "I changed my mind, show me GEWORLD instead." with prior history of a time constraint)

To run the test suite:
```bash
pytest tests/test_agent.py -v
```