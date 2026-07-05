import os as _os

# MLflow's HTTP client bakes its retry/timeout config into a function's
# default argument value at the moment `mlflow.utils.rest_utils` is first
# imported - not when the request is actually made. So these env vars have
# to be set before `mlflow` is imported ANYWHERE in this process, which
# means here, before this package's first import below (agent.py imports
# mlflow directly, and would otherwise lock in the library's defaults: a
# 120s timeout x 7 retries, i.e. a tracking server that's down or missing
# can silently hang for 10+ minutes instead of failing in a few seconds).
# setdefault() so a real deployment can still override via its own env.
_os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "5")
_os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "1")

from .agent import ask, generate_sql, run_query

__all__ = ["ask", "generate_sql", "run_query"]
