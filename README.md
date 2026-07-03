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
2. Load the data
```powershell
sqlite course_offerings.db < course_offerings_inserts.sql
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

### Run

```bash
python demo_sql_agent.py "What GE subjects can I take between 12:30pm and 16:00 that I haven't taken, given I've taken GEWORLD LCFAITH LCENWRD?"

# or interactively:
python demo_sql_agent.py
```

## Repo structure

```
data/
  course_offerings_inserts.sql    generated CREATE TABLE + INSERT statements
course_offerings.db                SQLite DB loaded from the .sql file above (644 sections)
sql_agent/
  config.py                       env-based settings (DeepSeek API key, model, DB path)
  db.py                           DB connection + schema introspection for prompting
  prompts.py                      system prompt, schema notes, few-shot examples
  agent.py                        ask(question) -> generated SQL -> guardrails -> execution
demo_sql_agent.py                  CLI to try the SQL Agent
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
Uses DeepSeek's API (OpenAI-compatible) via the `openai` SDK, plain Python
(no agent framework), with a guardrail that only allows single `SELECT`
statements against the `course_offerings` table.

Other modules (Chat UI, API endpoint) should call `sql_agent.ask(question)`,
which returns `{question, reasoning, sql, raw_response, rows, error}`.