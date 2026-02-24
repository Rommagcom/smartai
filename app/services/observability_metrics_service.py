from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from threading import Lock


class ObservabilityMetricsService:
    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[str, int] = defaultdict(int)
        self._latency: dict[str, dict[str, float]] = defaultdict(
            lambda: {"count": 0, "sum_ms": 0.0, "max_ms": 0.0}
        )

    @staticmethod
    def _key(component: str, operation: str) -> str:
        return f"{component}.{operation}"

    def record(self, *, component: str, operation: str, success: bool, latency_ms: float) -> None:
        key = self._key(component, operation)
        with self._lock:
            self._counters[f"{key}.total"] += 1
            self._counters[f"{key}.success" if success else f"{key}.failed"] += 1

            metric = self._latency[key]
            metric["count"] += 1
            metric["sum_ms"] += float(latency_ms)
            metric["max_ms"] = max(metric["max_ms"], float(latency_ms))

    def increment(self, metric_name: str, value: int = 1) -> None:
        with self._lock:
            self._counters[metric_name] += int(value)

    def snapshot(self) -> dict:
        with self._lock:
            latency = {
                key: {
                    "count": int(value["count"]),
                    "avg_ms": (float(value["sum_ms"]) / float(value["count"])) if value["count"] else 0.0,
                    "max_ms": float(value["max_ms"]),
                }
                for key, value in self._latency.items()
            }
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "counters": dict(self._counters),
                "latency": latency,
            }


observability_metrics_service = ObservabilityMetricsService()
