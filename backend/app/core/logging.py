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
    """Suppress noisy, expected uvicorn access-log lines for service endpoints."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "worker-results/poll" in msg and "200" in msg:
            return False
        # Prometheus metrics endpoint is commonly probed without auth.
        # Keep unauthorized probes out of INFO access logs.
        if "/observability/metrics/prometheus" in msg and "401" in msg:
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

    level_name = str(settings.OBS_LOG_LEVEL or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root_logger.setLevel(level)
    root_logger.addHandler(handler)

    third_party_level_name = str(settings.OBS_THIRD_PARTY_LOG_LEVEL or "WARNING").upper()
    third_party_level = getattr(logging, third_party_level_name, logging.WARNING)
    for logger_name in (
        "LiteLLM",
        "litellm",
        "httpcore",
        "httpx",
        "aiohttp",
        "urllib3",
    ):
        logging.getLogger(logger_name).setLevel(third_party_level)

    # Trafilatura can be extremely noisy on DEBUG and pollutes request logs.
    # Keep it informative by default unless explicit deep-debug is enabled.
    trafilatura_level = logging.DEBUG if settings.DEV_VERBOSE_LOGGING else logging.INFO
    for logger_name in (
        "trafilatura",
        "trafilatura.main_extractor",
        "trafilatura.htmlprocessing",
        "trafilatura.readability_lxml",
        "trafilatura.external",
        "trafilatura.core",
    ):
        logging.getLogger(logger_name).setLevel(trafilatura_level)

    # Reduce poll endpoint noise in uvicorn access log
    logging.getLogger("uvicorn.access").addFilter(_PollAccessFilter())
