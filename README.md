# STAI100 Capstone: Course Suggestion Agent

An LLM-powered assistant that helps students pick General Education / elective
subjects (course code, section, schedule, room, teacher) based on natural
language requests, backed by a real course-offering dataset for the term.



### Setup
Do not forget to download the [dataset](#dataset) accordingly. It should be placed under `/data`
1. Install SQLite by running this in powershell
```ps
winget install SQLite.SQLite
```
2. Load the data. PowerShell doesn't support `<` input redirection, so use
sqlite3's `.read` command instead of piping the file in:
```powershell
sqlite3 course_offerings.db ".read course_offerings_inserts.sql"
```
3. Verify it works (should return 644)
```powershell
sqlite3 course_offerings.db "SELECT COUNT(*) FROM course_offerings;"
```
4. Setup python requirements and environment
```bash
pip install -r requirements.txt
cp .env.example .env   # then edit .env and add your DEEPSEEK_API_KEY
```
5. Start the MLflow tracking server (for LLMOps monitoring - latency, token
usage, and traces of every question). Leave this running in its own terminal:
```bash
uvx mlflow server
```

### Run

```bash
python demo_sql_agent.py "What GE subjects can I take between 12:30pm and 16:00 that I haven't taken, given I've taken GEWORLD LCFAITH LCENWRD?"

# or interactively (with short-term conversation memory):
python demo_sql_agent.py
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
uvicorn api:app --reload
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
`{question, reasoning, sql, rows, error, updated_history}`. Guardrail rejections and SQL errors still come
back as a normal 200 with `error` populated; only a genuine upstream
failure (DeepSeek unreachable/timed out) returns a 502.

## Repo structure

```
data/
  course_offerings_inserts.sql    generated CREATE TABLE + INSERT statements
course_offerings.db                SQLite DB loaded from the .sql file above (644 sections)
sql_agent/
  config.py                       env-based settings (DeepSeek API key, model, DB path, MLflow)
  db.py                           DB connection + schema introspection for prompting
  prompts.py                      system prompt, schema notes, few-shot examples
  monitoring.py                   MLflow tracing setup (latency, token usage, traces)
  agent.py                        ask(question) -> generated SQL -> guardrails -> execution
demo_sql_agent.py                  CLI to try the SQL Agent
api.py                             REST API (FastAPI) wrapping sql_agent.ask()
requirements.txt
.env.example                       copy to .env and fill in your DeepSeek API key
```

## Dataset

`course_offerings` is built from the DLSU's course offerings from the previous year.
Each row is one section: course code,
teacher, section, up to two weekly meeting times (day + start/end), room,
and remarks (e.g. FULL, HYBRID, ONLINE). Students refer to every course code
here as a "GE subject" regardless of whether it's GE- or LC-prefixed — both
are the same general-elective pool.

Dataset may be downloaded [from this Google Drive](https://drive.google.com/file/d/11mMwMHjzWzoACcQR6Y1-s-W2kTVVloX9/view?usp=sharing)
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