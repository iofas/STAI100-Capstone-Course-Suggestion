"""
Configuration for the SQL Agent module.

Reads settings from environment variables (loaded from a local .env file if
present). Copy .env.example to .env and fill in your DeepSeek API key before
running anything in this package.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# DeepSeek exposes an OpenAI-compatible API, so we use the `openai` SDK
# pointed at DeepSeek's base URL instead of OpenAI's.
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# deepseek-chat / deepseek-reasoner are deprecated 2026-07-24; use the
# current model names going forward.
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

# The openai SDK defaults to a 10-minute timeout, which makes a slow/stuck
# API call look like a silent freeze instead of a clear error. Fail faster
# so it's obvious when DeepSeek isn't responding.
DEEPSEEK_TIMEOUT_SECONDS = float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "45"))

# Path to the SQLite database generated from data/unformatted/*.txt
# (see course_offerings_inserts.sql at the repo root).
DB_PATH = os.getenv("COURSE_DB_PATH", "course_offerings.db")

# course_offerings holds multiple terms (DLSU term codes, e.g. 1241, 1261).
# The scheduler/agent must never mix terms, so every candidate query is scoped
# to exactly one term. Defaults to the incoming term students schedule for;
# override with SCHEDULE_TERM to run against archived data (e.g. 1241).
SCHEDULE_TERM = os.getenv("SCHEDULE_TERM", "1261")

# LLMOps monitoring (MLflow). Start the tracking server separately with
# `uvx mlflow server` (defaults to http://localhost:5000) before running the
# agent, otherwise traces just fail to upload instead of crashing the app.
MLFLOW_TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000")
MLFLOW_EXPERIMENT_NAME = os.getenv("MLFLOW_EXPERIMENT_NAME", "sql_agent")


def require_api_key() -> str:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is not set. Copy .env.example to .env and add "
            "your DeepSeek API key, or export DEEPSEEK_API_KEY in your shell."
        )
    return DEEPSEEK_API_KEY
