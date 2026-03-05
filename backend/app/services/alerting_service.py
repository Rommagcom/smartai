from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import logging
from threading import Lock
from typing import Any

from app.core.config import settings
from app.services.observability_metrics_service import observability_metrics_service

logger = logging.getLogger("observability.alerts")


class AlertingService:
    def __init__(self) -> None:
        self._lock = Lock()
        self._items: deque[dict[str, Any]] = deque(maxlen=max(10, settings.OBS_ALERT_BUFFER_SIZE))

    def emit(self, *, component: str, message: str, severity: str = "warning", details: dict | None = None) -> None:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "component": str(component),
            "severity": str(severity),
            "message": str(message),
            "details": details or {},
        }
        with self._lock:
            self._items.appendleft(payload)

        observability_metrics_service.increment(f"alerts.{component}.{severity}")
        log_context = {"context": payload}
        if severity.lower() == "critical":
            logger.error("alert emitted", extra=log_context)
        else:
            logger.warning("alert emitted", extra=log_context)

    def list_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        count = max(1, min(limit, settings.OBS_ALERT_BUFFER_SIZE))
        with self._lock:
            return list(self._items)[:count]


alerting_service = AlertingService()
