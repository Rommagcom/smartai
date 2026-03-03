from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import re
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

    @staticmethod
    def _sanitize_metric_name(name: str) -> str:
        normalized = re.sub(r"\W", "_", str(name or ""))
        if not normalized:
            return "metric"
        if normalized[0].isdigit():
            return f"m_{normalized}"
        return normalized

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

    def to_prometheus(self) -> str:
        with self._lock:
            lines: list[str] = []

            lines.append("# HELP assistant_observability_up Observability exporter availability")
            lines.append("# TYPE assistant_observability_up gauge")
            lines.append("assistant_observability_up 1")

            for name, value in sorted(self._counters.items()):
                metric = self._sanitize_metric_name(f"assistant_{name}")
                lines.append(f"# TYPE {metric} counter")
                lines.append(f"{metric} {int(value)}")

            for key, value in sorted(self._latency.items()):
                base = self._sanitize_metric_name(f"assistant_{key}_latency_ms")
                count = int(value["count"])
                sum_ms = float(value["sum_ms"])
                max_ms = float(value["max_ms"])
                avg_ms = (sum_ms / count) if count else 0.0

                lines.append(f"# TYPE {base}_count counter")
                lines.append(f"{base}_count {count}")

                lines.append(f"# TYPE {base}_sum gauge")
                lines.append(f"{base}_sum {sum_ms:.6f}")

                lines.append(f"# TYPE {base}_avg gauge")
                lines.append(f"{base}_avg {avg_ms:.6f}")

                lines.append(f"# TYPE {base}_max gauge")
                lines.append(f"{base}_max {max_ms:.6f}")

            generated = int(datetime.now(timezone.utc).timestamp())
            lines.append("# TYPE assistant_observability_generated_at gauge")
            lines.append(f"assistant_observability_generated_at {generated}")
            return "\n".join(lines) + "\n"


observability_metrics_service = ObservabilityMetricsService()
