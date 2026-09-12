import contextvars
import json
import logging
import sys
from datetime import datetime, timezone

correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


def log_event(event: str, **fields) -> None:
    """Emit one line of structured JSON, tagged with the request's correlation id."""
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": "info",
        "event": event,
        "correlation_id": correlation_id_var.get(),
        **fields,
    }
    logging.getLogger("wallet_service").info(json.dumps(payload, default=str))
