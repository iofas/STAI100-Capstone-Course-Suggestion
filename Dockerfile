# Single image used to run BOTH services (api and chatui) - which one
# actually starts is decided by the `command:` in docker-compose.yml (or an
# override on `docker run`). This avoids maintaining two near-identical
# images just because they run different entrypoints.
FROM python:3.12-slim

WORKDIR /app

# sqlite3 CLI is included so course_offerings_inserts.sql can be loaded
# inside the container/volume if needed (see README instructions).
RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Install deps first so this layer is cached across code-only changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000 8501

# Default: run the API. docker-compose.yml overrides `command:` for the
# chatui service to run Streamlit instead.
CMD ["uvicorn", "apps.api:app", "--host", "0.0.0.0", "--port", "8000"]
