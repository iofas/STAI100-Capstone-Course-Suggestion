"""
LLMOps monitoring for the SQL Agent module, via MLflow Tracing.

This wires up the "LLMOps Monitoring" checklist item: every LLM call gets
its latency, token usage, prompt, and response logged automatically, and
every `ask()` invocation gets logged as a trace with the question,
reasoning, generated SQL, row count, and any guardrail/execution error
attached - viewable in the MLflow UI.

Start the tracking server once, in a separate terminal, before running the
agent:

    uvx mlflow server

(defaults to http://localhost:5000; override with MLFLOW_TRACKING_URI if
you run it elsewhere). This module only points the app at that server and
turns on logging - it doesn't start the server itself.

Note: the MLFLOW_HTTP_REQUEST_TIMEOUT / MLFLOW_HTTP_REQUEST_MAX_RETRIES env
vars that make a down/missing tracking server fail fast (instead of hanging
for 10+ minutes) are set in sql_agent/__init__.py, not here - they have to
be in place before `mlflow` is imported anywhere in the process, which is
earlier than this module gets a chance to run. See the comment there.
"""
import logging

import mlflow

from .config import MLFLOW_EXPERIMENT_NAME, MLFLOW_TRACKING_URI

logger = logging.getLogger(__name__)

_configured = False


def setup_tracing() -> None:
    """Idempotent: point MLflow at the tracking server, select/create the
    experiment, and autolog the OpenAI SDK (which is what talks to
    DeepSeek's OpenAI-compatible API), capturing latency and token usage
    for every LLM call with no extra instrumentation needed."""
    global _configured
    if _configured:
        return

    try:
        mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
        mlflow.set_experiment(MLFLOW_EXPERIMENT_NAME)
        mlflow.openai.autolog()
    except Exception:
        # Don't let monitoring setup take the app down if the tracking
        # server isn't running yet - just log and move on without tracing.
        logger.warning(
            "MLflow tracing could not be configured (tracking server at %s "
            "unreachable?). Continuing without tracing.",
            MLFLOW_TRACKING_URI,
            exc_info=True,
        )
        return

    _configured = True
