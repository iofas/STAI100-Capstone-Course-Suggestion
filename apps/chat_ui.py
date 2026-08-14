"""
Chat UI for the STAI100 Course Suggestion Agent (the "Chat UI" module).

A thin Streamlit front-end over the existing REST API (apps/api.py). It does
NOT import sql_agent directly - it talks to the API endpoint over HTTP, the
same way any external client would, so the API Endpoint module stays the
single real entry point into the agent.

Run locally from the repo root (with the API already running separately):
    streamlit run apps/chat_ui.py

Run in Docker: see Dockerfile / docker-compose.yml. The API_URL env var
controls which API instance this UI talks to (defaults to localhost:8000,
overridden to http://api:8000 inside docker-compose).
"""
import os

import requests
import streamlit as st

API_URL = os.getenv("API_URL", "http://localhost:8000")
REQUEST_TIMEOUT_SECONDS = 60

st.set_page_config(page_title="DLSU Course Suggestion Agent", page_icon="🎓", layout="centered")

# --- Styling --------------------------------------------------------------
# Custom CSS layered on top of the theme in .streamlit/config.toml: a green
# gradient banner (DLSU brand green) for the title, and colored badge styles
# so reasoning / SQL / results read as visually distinct sections.
st.markdown(
    """
    <style>
    .agent-banner {
        background: linear-gradient(135deg, #0b6e4f 0%, #16a34a 55%, #facc15 160%);
        padding: 1.4rem 1.6rem;
        border-radius: 14px;
        margin-bottom: 1.2rem;
    }
    .agent-banner h1 {
        color: white;
        margin: 0;
        font-size: 1.8rem;
    }
    .agent-banner p {
        color: rgba(255,255,255,0.92);
        margin: 0.3rem 0 0 0;
        font-size: 0.95rem;
    }
    .badge {
        display: inline-block;
        padding: 0.15rem 0.6rem;
        border-radius: 999px;
        font-size: 0.72rem;
        font-weight: 600;
        letter-spacing: 0.03em;
        text-transform: uppercase;
        margin-bottom: 0.4rem;
    }
    .badge-reasoning { background: #ede9fe; color: #6d28d9; }
    .badge-sql       { background: #dbeafe; color: #1d4ed8; }
    .badge-results   { background: #dcfce7; color: #15803d; }
    .badge-schedule  { background: #fef3c7; color: #b45309; }
    .badge-verified  { background: #dcfce7; color: #15803d; }
    .filter-guide {
        background: #0f2b1c;
        border: 1px solid #1f5137;
        border-radius: 10px;
        padding: 0.8rem 0.9rem;
        font-size: 0.86rem;
        line-height: 1.35rem;
        color: #d5efe0;
    }
    .filter-guide b { color: #ffffff; }
    .filter-guide code {
        background: #14532d;
        color: #bbf7d0;
        padding: 0 0.25rem;
        border-radius: 4px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# The schedule-building guide, surfaced on demand from the ℹ️ popover above the
# chat input (it used to live permanently in the sidebar).
FILTER_GUIDE_HTML = """
<div class="filter-guide">
<b>Just describe the schedule you want in plain language</b> — there's no form.
A deterministic optimizer builds a verified, conflict-free timetable (and tells
you what it relaxed if your request is over-tight). You can <b>filter / shape</b>
it by:
<ul style="margin:0.4rem 0 0 -0.6rem; padding-left:1rem;">
  <li><b>Which subjects</b> — name them (<code>GEARTAP, GEWORLD, LCFAITH</code>)
      or ask for a number (<code>"give me 4 GE subjects"</code>).</li>
  <li><b>Class start / end time</b> — <code>"nothing before 9am or after 3pm"</code>.</li>
  <li><b>Timeslot window</b> — <code>"only classes between 12:30 and 16:00"</code>.</li>
  <li><b>Days on campus</b> — <code>"only Mondays and Wednesdays"</code>,
      <code>"keep me to 3 days a week"</code>.</li>
  <li><b>Max classes per day</b> — <code>"no more than 2 classes in one day"</code>.</li>
  <li><b>Minimize time at school / no gaps</b> —
      <code>"pack my days, I hate gaps"</code>.</li>
  <li><b>Already taken / prerequisites</b> —
      <code>"I've already passed LCLSONE and GEUSELF"</code>
      (excluded, and prereqs enforced).</li>
</ul>
<div style="margin-top:0.5rem; color:#86efac;">
  Combine any of these in one sentence — e.g.
  <code>"Build me a compact schedule of 4 subjects, Mon/Wed only,
  nothing after 4pm, max 2 a day."</code>
</div>
<div style="margin-top:0.6rem; color:#9fb8ab; font-size:0.8rem;">
  Note: College of Liberal Arts–exclusive GEs (e.g. GELITWO, LCFILIC) are not
  included since not everyone takes them. The NSTP- and LASARE series are also
  excluded due to the nature of their schedules.
</div>
</div>
"""

# --- Sidebar: connection status + controls -----------------------------
with st.sidebar:
    st.header("Settings")
    st.text(f"API: {API_URL}")

    try:
        health = requests.get(f"{API_URL}/health", timeout=5)
        if health.ok:
            st.success("API is reachable")
        else:
            st.error(f"API returned {health.status_code}")
    except requests.exceptions.RequestException:
        st.error("API is not reachable")
        st.caption(
            "Start it with `uvicorn api:app --reload` (or via docker-compose) "
            "before asking questions."
        )

    if st.button("Clear conversation"):
        st.session_state.messages = []
        st.session_state.history = []
        st.rerun()

st.markdown(
    """
    <div class="agent-banner">
        <h1>🎓 DLSU Course Suggestion Agent</h1>
        <p>Ask about GE/elective sections, or have it <b>build you a conflict-free schedule</b> - in plain language.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

if "messages" not in st.session_state:
    st.session_state.messages = []  # what gets rendered in the chat window
if "history" not in st.session_state:
    st.session_state.history = []  # raw {role, content} history sent to the API


def render_schedule_extras(msg: dict) -> None:
    """Render the scheduler's negotiation trace + correctness check, if any."""
    if msg.get("mode") != "schedule":
        return
    relaxations = msg.get("relaxations") or []
    dropped = msg.get("dropped_courses") or []
    if relaxations:
        st.warning(
            "Constraints relaxed to reach a valid schedule:\n"
            + "\n".join(f"- {r}" for r in relaxations)
        )
    if dropped:
        st.error("Couldn't fit: " + ", ".join(dropped))
    criteria = msg.get("criteria") or {}
    if criteria.get("correct"):
        st.markdown(
            '<span class="badge badge-verified">✓ Verified</span>',
            unsafe_allow_html=True,
        )
        st.caption(
            "No time conflicts (C1), no duplicates (C2), every section and its "
            "times match the catalog (C3), only requested courses (C4), all "
            "hard constraints hold (C5), every course is one you're eligible "
            "for (C6), every section is attendable (C7)."
        )

# --- Render existing conversation ---------------------------------------
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        is_schedule = msg.get("mode") == "schedule"
        if msg["role"] == "assistant" and msg["content"]:
            if is_schedule:
                st.markdown('<span class="badge badge-schedule">Schedule</span>', unsafe_allow_html=True)
            else:
                st.markdown('<span class="badge badge-reasoning">Reasoning</span>', unsafe_allow_html=True)
        st.markdown(msg["content"])
        if msg.get("sql"):
            st.markdown('<span class="badge badge-sql">SQL</span>', unsafe_allow_html=True)
            with st.expander("Generated SQL", expanded=False):
                st.code(msg["sql"], language="sql")
        rows = msg.get("rows")
        if rows:
            label = "Your schedule" if is_schedule else "Results"
            st.markdown(f'<span class="badge badge-results">{label}</span>', unsafe_allow_html=True)
            st.dataframe(rows, use_container_width=True)
        elif rows is not None:
            st.info("No matching sections found.")
        render_schedule_extras(msg)

# --- Info popover + model disclaimer, just above the chat input ----------
info_col, disclaimer_col = st.columns([1, 4], vertical_alignment="center")
with info_col:
    with st.popover("ℹ️ Schedule tips", use_container_width=True):
        st.markdown(FILTER_GUIDE_HTML, unsafe_allow_html=True)
with disclaimer_col:
    st.caption(
        "⚡ Powered by DeepSeek-v4. Suggestions are AI-generated and may be "
        "inaccurate — always verify against the official DLSU enlistment system."
    )

# --- Handle new input -----------------------------------------------------
question = st.chat_input(
    "e.g. Build me a compact schedule of GEARTAP, GEWORLD and LCFAITH before 3pm"
)

if question:
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        data = None
        with st.spinner("Thinking..."):
            try:
                resp = requests.post(
                    f"{API_URL}/ask",
                    json={"question": question, "history": st.session_state.history},
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                resp.raise_for_status()
                data = resp.json()
            except requests.exceptions.RequestException as exc:
                st.error(f"Couldn't reach the API at {API_URL}: {exc}")

        if data is not None:
            reply_text = data.get("reasoning") or ""
            error = data.get("error")
            is_schedule = data.get("mode") == "schedule"

            if error:
                st.warning(error)
            if reply_text:
                if is_schedule:
                    st.markdown('<span class="badge badge-schedule">Schedule</span>', unsafe_allow_html=True)
                else:
                    st.markdown('<span class="badge badge-reasoning">Reasoning</span>', unsafe_allow_html=True)
                st.markdown(reply_text)

            sql = data.get("sql")
            if sql:
                st.markdown('<span class="badge badge-sql">SQL</span>', unsafe_allow_html=True)
                with st.expander("Generated SQL", expanded=False):
                    st.code(sql, language="sql")

            rows = data.get("rows")
            if rows:
                label = "Your schedule" if is_schedule else "Results"
                st.markdown(f'<span class="badge badge-results">{label}</span>', unsafe_allow_html=True)
                st.dataframe(rows, use_container_width=True)
            elif rows is not None:
                st.info("No matching sections found.")

            assistant_msg = {
                "role": "assistant",
                "content": reply_text or (error or "(no response)"),
                "sql": sql,
                "rows": rows,
                "mode": data.get("mode", "lookup"),
                "relaxations": data.get("relaxations", []),
                "dropped_courses": data.get("dropped_courses", []),
                "criteria": data.get("criteria"),
            }
            render_schedule_extras(assistant_msg)
            st.session_state.messages.append(assistant_msg)
            st.session_state.history = data.get("updated_history", st.session_state.history)
