from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.core.config import settings


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        context = getattr(record, "context", None)
        if isinstance(context, dict):
            payload.update(context)

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


class _PollAccessFilter(logging.Filter):
    """Suppress noisy 200 OK lines for the worker-results/poll endpoint."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "worker-results/poll" in msg and "200" in msg:
            return False
        return True


def setup_logging() -> None:
    root_logger = logging.getLogger()
    if root_logger.handlers:
        return

    handler = logging.StreamHandler()
    if settings.OBS_LOG_JSON:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))

    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)

    # Reduce poll endpoint noise in uvicorn access log
    logging.getLogger("uvicorn.access").addFilter(_PollAccessFilter())
