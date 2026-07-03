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

# Path to the SQLite database generated from data/unformatted/*.txt
# (see course_offerings_inserts.sql at the repo root).
DB_PATH = os.getenv("COURSE_DB_PATH", "course_offerings.db")


def require_api_key() -> str:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is not set. Copy .env.example to .env and add "
            "your DeepSeek API key, or export DEEPSEEK_API_KEY in your shell."
        )
    return DEEPSEEK_API_KEY
