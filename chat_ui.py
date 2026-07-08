"""
Chat UI for the STAI100 Course Suggestion Agent (the "Chat UI" module).

A thin Streamlit front-end over the existing REST API (api.py). It does NOT
import sql_agent directly - it talks to the API endpoint over HTTP, the same
way any external client would, so the API Endpoint module stays the single
real entry point into the agent.

Run locally (with the API already running separately):
    streamlit run chat_ui.py

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
    </style>
    """,
    unsafe_allow_html=True,
)

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
        <p>Ask about GE/elective sections - schedules, teachers, prerequisites - in plain language.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

if "messages" not in st.session_state:
    st.session_state.messages = []  # what gets rendered in the chat window
if "history" not in st.session_state:
    st.session_state.history = []  # raw {role, content} history sent to the API

# --- Render existing conversation ---------------------------------------
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        if msg["role"] == "assistant" and msg["content"]:
            st.markdown('<span class="badge badge-reasoning">Reasoning</span>', unsafe_allow_html=True)
        st.markdown(msg["content"])
        if msg.get("sql"):
            st.markdown('<span class="badge badge-sql">SQL</span>', unsafe_allow_html=True)
            with st.expander("Generated SQL", expanded=False):
                st.code(msg["sql"], language="sql")
        rows = msg.get("rows")
        if rows:
            st.markdown('<span class="badge badge-results">Results</span>', unsafe_allow_html=True)
            st.dataframe(rows, use_container_width=True)
        elif rows is not None:
            st.info("No matching sections found.")

# --- Handle new input -----------------------------------------------------
question = st.chat_input("e.g. What GE subjects can I take between 12:30pm and 4pm?")

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

            if error:
                st.warning(error)
            if reply_text:
                st.markdown('<span class="badge badge-reasoning">Reasoning</span>', unsafe_allow_html=True)
                st.markdown(reply_text)

            sql = data.get("sql")
            if sql:
                st.markdown('<span class="badge badge-sql">SQL</span>', unsafe_allow_html=True)
                with st.expander("Generated SQL", expanded=False):
                    st.code(sql, language="sql")

            rows = data.get("rows")
            if rows:
                st.markdown('<span class="badge badge-results">Results</span>', unsafe_allow_html=True)
                st.dataframe(rows, use_container_width=True)
            elif rows is not None:
                st.info("No matching sections found.")

            st.session_state.messages.append(
                {
                    "role": "assistant",
                    "content": reply_text or (error or "(no response)"),
                    "sql": sql,
                    "rows": rows,
                }
            )
            st.session_state.history = data.get("updated_history", st.session_state.history)
