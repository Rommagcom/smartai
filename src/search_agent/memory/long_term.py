from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib import error as urllib_error
from urllib import request as urllib_request

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from search_agent.config import Settings


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LongTermMemoryItem:
    content: str
    score: float
    created_at: str


@dataclass(slots=True)
class LongTermMemoryReliabilityPolicy:
    embedding_timeout_seconds: int = 20
    embedding_retry_attempts: int = 3
    embedding_retry_base_delay_seconds: float = 0.5
    embedding_retry_max_delay_seconds: float = 4.0
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_recovery_seconds: int = 60
    retention_days: int = 90
    archive_batch_size: int = 500
    maintenance_interval_seconds: int = 300


class LongTermMemoryStore:
    def __init__(
        self,
        *,
        database_url: str,
        ollama_base_url: str,
        embedding_model: str,
        api_key: str | None,
        top_k: int,
        max_entry_chars: int,
        reliability_policy: LongTermMemoryReliabilityPolicy,
    ) -> None:
        self.database_url = database_url
        self.ollama_base_url = ollama_base_url.rstrip("/")
        self.embedding_model = self._normalize_embedding_model(embedding_model)
        self.api_key = (api_key or "").strip() or None
        self.top_k = max(1, top_k)
        self.max_entry_chars = max(256, max_entry_chars)
        self.embedding_timeout_seconds = max(1, int(reliability_policy.embedding_timeout_seconds))
        self.embedding_retry_attempts = max(1, int(reliability_policy.embedding_retry_attempts))
        self.embedding_retry_base_delay_seconds = max(
            0.05,
            float(reliability_policy.embedding_retry_base_delay_seconds),
        )
        self.embedding_retry_max_delay_seconds = max(
            self.embedding_retry_base_delay_seconds,
            float(reliability_policy.embedding_retry_max_delay_seconds),
        )
        self.circuit_breaker_failure_threshold = max(
            1,
            int(reliability_policy.circuit_breaker_failure_threshold),
        )
        self.circuit_breaker_recovery_seconds = max(
            5,
            int(reliability_policy.circuit_breaker_recovery_seconds),
        )
        self.retention_days = max(0, int(reliability_policy.retention_days))
        self.archive_batch_size = max(1, int(reliability_policy.archive_batch_size))
        self.maintenance_interval_seconds = max(
            5,
            int(reliability_policy.maintenance_interval_seconds),
        )

        self._state_lock = threading.Lock()
        self._consecutive_embedding_failures = 0
        self._breaker_open_until: datetime | None = None
        self._last_maintenance_at: datetime | None = None
        self._metrics: dict[str, int] = {
            "embedding_success_total": 0,
            "embedding_failure_total": 0,
            "embedding_retries_total": 0,
            "circuit_open_total": 0,
            "circuit_open_rejections_total": 0,
            "recall_total": 0,
            "recall_hits_total": 0,
            "recall_empty_total": 0,
            "recall_degraded_total": 0,
            "remember_degraded_total": 0,
            "retention_runs_total": 0,
            "retention_archived_total": 0,
        }

        self.engine: Engine = create_engine(
            self._normalize_db_url(database_url),
            future=True,
            pool_pre_ping=True,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "LongTermMemoryStore | None":
        if not settings.enable_long_term_memory:
            return None

        raw_db_url = (settings.long_term_memory_database_url or "").strip()
        if not raw_db_url:
            logger.warning("Long-term memory disabled: LONG_TERM_MEMORY_DATABASE_URL is empty.")
            return None

        try:
            return cls(
                database_url=raw_db_url,
                ollama_base_url=settings.ollama_base_url or "http://localhost:11434",
                embedding_model=settings.long_term_memory_embedding_model,
                api_key=settings.ollama_api_key,
                top_k=settings.long_term_memory_top_k,
                max_entry_chars=settings.long_term_memory_max_entry_chars,
                reliability_policy=LongTermMemoryReliabilityPolicy(
                    embedding_timeout_seconds=settings.long_term_memory_embedding_timeout_seconds,
                    embedding_retry_attempts=settings.long_term_memory_embedding_retry_attempts,
                    embedding_retry_base_delay_seconds=settings.long_term_memory_embedding_retry_base_delay_seconds,
                    embedding_retry_max_delay_seconds=settings.long_term_memory_embedding_retry_max_delay_seconds,
                    circuit_breaker_failure_threshold=settings.long_term_memory_circuit_breaker_failure_threshold,
                    circuit_breaker_recovery_seconds=settings.long_term_memory_circuit_breaker_recovery_seconds,
                    retention_days=settings.long_term_memory_retention_days,
                    archive_batch_size=settings.long_term_memory_archive_batch_size,
                    maintenance_interval_seconds=settings.long_term_memory_maintenance_interval_seconds,
                ),
            )
        except Exception as exc:
            logger.warning("Long-term memory initialization failed: %s", exc)
            return None

    @staticmethod
    def _normalize_db_url(value: str) -> str:
        db_url = value.strip()
        if db_url.startswith("postgres://"):
            return "postgresql+psycopg://" + db_url.removeprefix("postgres://")
        if db_url.startswith("postgresql://"):
            return "postgresql+psycopg://" + db_url.removeprefix("postgresql://")
        return db_url

    @staticmethod
    def _normalize_embedding_model(value: str) -> str:
        model = value.strip()
        if not model:
            return "nomic-embed-text:latest"
        if model == "nomic-embed-text":
            return "nomic-embed-text:latest"
        return model

    @staticmethod
    def _vector_literal(vector: list[float]) -> str:
        return "[" + ",".join(f"{float(v):.8f}" for v in vector) + "]"

    def _is_circuit_open(self) -> bool:
        now = datetime.now(UTC)
        with self._state_lock:
            if self._breaker_open_until is None:
                return False
            if now >= self._breaker_open_until:
                self._breaker_open_until = None
                return False
            self._metrics["circuit_open_rejections_total"] += 1
            return True

    def _mark_embedding_success(self, *, retries_used: int) -> None:
        with self._state_lock:
            self._consecutive_embedding_failures = 0
            self._breaker_open_until = None
            self._metrics["embedding_success_total"] += 1
            self._metrics["embedding_retries_total"] += max(0, retries_used)

    def _mark_embedding_failure(self) -> None:
        now = datetime.now(UTC)
        with self._state_lock:
            self._consecutive_embedding_failures += 1
            self._metrics["embedding_failure_total"] += 1
            if self._consecutive_embedding_failures >= self.circuit_breaker_failure_threshold:
                self._breaker_open_until = now.replace(microsecond=0) + timedelta(seconds=self.circuit_breaker_recovery_seconds)
                self._metrics["circuit_open_total"] += 1

    def _embed_text(self, text_value: str) -> list[float]:
        if self._is_circuit_open():
            raise RuntimeError("embedding unavailable: circuit breaker is open")

        payload = {
            "model": self.embedding_model,
            "prompt": text_value,
        }
        req = urllib_request.Request(
            url=f"{self.ollama_base_url}/api/embeddings",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
            method="POST",
        )
        last_error: Exception | None = None

        for attempt in range(1, self.embedding_retry_attempts + 1):
            try:
                with urllib_request.urlopen(req, timeout=self.embedding_timeout_seconds) as response:
                    body = response.read().decode("utf-8")

                parsed = json.loads(body)
                embedding = parsed.get("embedding")
                if not isinstance(embedding, list) or not embedding:
                    raise RuntimeError("embedding response does not contain vector")

                normalized: list[float] = []
                for item in embedding:
                    normalized.append(float(item))

                self._mark_embedding_success(retries_used=attempt - 1)
                return normalized
            except (urllib_error.URLError, TimeoutError, ValueError, RuntimeError) as exc:
                last_error = exc
                if attempt >= self.embedding_retry_attempts:
                    break
                delay = min(
                    self.embedding_retry_max_delay_seconds,
                    self.embedding_retry_base_delay_seconds * (2 ** (attempt - 1)),
                )
                time.sleep(delay)

        self._mark_embedding_failure()
        raise RuntimeError(f"embedding request failed after retries: {last_error}") from last_error

    def _record_recall_quality(
        self,
        *,
        org_id: str,
        user_id: int,
        chat_id: int,
        query_text: str,
        result_count: int,
        top_score: float | None,
        degraded: bool,
        error_text: str | None,
    ) -> None:
        with self._state_lock:
            self._metrics["recall_total"] += 1
            if result_count > 0:
                self._metrics["recall_hits_total"] += 1
            else:
                self._metrics["recall_empty_total"] += 1
            if degraded:
                self._metrics["recall_degraded_total"] += 1

        safe_query = query_text.strip()[:2000]
        safe_error = (error_text or "").strip()[:1000]
        stmt = text(
            """
            INSERT INTO long_term_recall_metrics (
                org_id, user_id, chat_id, query_text, result_count,
                top_score, degraded, error_text, created_at
            )
            VALUES (
                :org_id, :user_id, :chat_id, :query_text, :result_count,
                :top_score, :degraded, :error_text, :created_at
            )
            """
        )
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    stmt,
                    {
                        "org_id": org_id,
                        "user_id": int(user_id),
                        "chat_id": int(chat_id),
                        "query_text": safe_query,
                        "result_count": int(result_count),
                        "top_score": float(top_score) if top_score is not None else None,
                        "degraded": bool(degraded),
                        "error_text": safe_error,
                        "created_at": datetime.now(UTC),
                    },
                )
        except Exception:
            logger.debug("long_term_recall_metrics insert skipped (table unavailable or query failed)")

    def _run_retention_maintenance_if_due(self) -> None:
        if self.retention_days <= 0:
            return

        now = datetime.now(UTC)
        with self._state_lock:
            last = self._last_maintenance_at
            if last is not None and (now - last).total_seconds() < self.maintenance_interval_seconds:
                return
            self._last_maintenance_at = now

        try:
            archived = self._archive_expired_memories()
            with self._state_lock:
                self._metrics["retention_runs_total"] += 1
                self._metrics["retention_archived_total"] += int(archived)
        except Exception as exc:
            logger.warning("Long-term memory retention maintenance failed: %s", exc)

    def _archive_expired_memories(self) -> int:
        stmt = text(
            """
            WITH moved AS (
                DELETE FROM long_term_memories
                WHERE id IN (
                    SELECT id
                    FROM long_term_memories
                    WHERE created_at < (now() - make_interval(days => :retention_days))
                    ORDER BY created_at ASC
                    LIMIT :batch_size
                )
                RETURNING id, org_id, user_id, chat_id, source, content, embedding, embedding_model, created_at
            ),
            inserted AS (
                INSERT INTO long_term_memories_archive (
                    id, org_id, user_id, chat_id, source, content, embedding, embedding_model, created_at, archived_at
                )
                SELECT id, org_id, user_id, chat_id, source, content, embedding, embedding_model, created_at, now()
                FROM moved
                RETURNING 1
            )
            SELECT count(*)::int AS archived_count FROM inserted
            """
        )
        with self.engine.begin() as conn:
            row = conn.execute(
                stmt,
                {
                    "retention_days": int(self.retention_days),
                    "batch_size": int(self.archive_batch_size),
                },
            ).mappings().first()
        if row is None:
            return 0
        return int(row.get("archived_count") or 0)

    def recall(
        self,
        *,
        org_id: str,
        user_id: int,
        chat_id: int,
        query_text: str,
        limit: int | None = None,
    ) -> list[LongTermMemoryItem]:
        normalized_query = query_text.strip()
        if not normalized_query:
            return []

        try:
            query_embedding = self._embed_text(normalized_query)
        except Exception as exc:
            logger.warning("Long-term recall degraded: embedding failed: %s", exc)
            self._record_recall_quality(
                org_id=org_id,
                user_id=user_id,
                chat_id=chat_id,
                query_text=normalized_query,
                result_count=0,
                top_score=None,
                degraded=True,
                error_text=f"embedding_failed: {exc}",
            )
            return []

        query_vector = self._vector_literal(query_embedding)
        row_limit = max(1, limit or self.top_k)

        stmt = text(
            """
            SELECT
                content,
                created_at::text AS created_at,
                1 - (embedding <=> CAST(:query_vector AS vector)) AS score
            FROM long_term_memories
            WHERE org_id = :org_id
                AND user_id = :user_id
                AND chat_id = :chat_id
            ORDER BY embedding <=> CAST(:query_vector AS vector)
            LIMIT :row_limit
            """
        )

        items: list[LongTermMemoryItem] = []
        try:
            with self.engine.connect() as conn:
                rows = conn.execute(
                    stmt,
                    {
                        "org_id": org_id,
                        "user_id": int(user_id),
                        "chat_id": int(chat_id),
                        "query_vector": query_vector,
                        "row_limit": row_limit,
                    },
                )
                for row in rows:
                    content = str(row.content or "").strip()
                    if not content:
                        continue
                    items.append(
                        LongTermMemoryItem(
                            content=content,
                            score=float(row.score or 0.0),
                            created_at=str(row.created_at or ""),
                        )
                    )
        except Exception as exc:
            logger.warning("Long-term recall degraded: query failed: %s", exc)
            self._record_recall_quality(
                org_id=org_id,
                user_id=user_id,
                chat_id=chat_id,
                query_text=normalized_query,
                result_count=0,
                top_score=None,
                degraded=True,
                error_text=f"query_failed: {exc}",
            )
            return []

        top_score = max((item.score for item in items), default=None)
        self._record_recall_quality(
            org_id=org_id,
            user_id=user_id,
            chat_id=chat_id,
            query_text=normalized_query,
            result_count=len(items),
            top_score=top_score,
            degraded=False,
            error_text=None,
        )

        return items

    def remember(
        self,
        *,
        org_id: str,
        user_id: int,
        chat_id: int,
        user_text: str,
        assistant_text: str,
        source: str = "chat",
    ) -> None:
        compact_user = user_text.strip()
        compact_assistant = assistant_text.strip()
        if not compact_user and not compact_assistant:
            return

        merged = f"User: {compact_user}\nAssistant: {compact_assistant}".strip()
        if len(merged) > self.max_entry_chars:
            merged = merged[: self.max_entry_chars]

        try:
            embedding = self._embed_text(merged)
        except Exception as exc:
            logger.warning("Long-term remember degraded: embedding failed: %s", exc)
            with self._state_lock:
                self._metrics["remember_degraded_total"] += 1
            return

        embedding_literal = self._vector_literal(embedding)

        stmt = text(
            """
            INSERT INTO long_term_memories (
                org_id, user_id, chat_id, source, content, embedding, embedding_model, created_at
            )
            VALUES (
                :org_id, :user_id, :chat_id, :source, :content, CAST(:embedding AS vector),
                :embedding_model, :created_at
            )
            """
        )

        with self.engine.begin() as conn:
            conn.execute(
                stmt,
                {
                    "org_id": org_id,
                    "user_id": int(user_id),
                    "chat_id": int(chat_id),
                    "source": source,
                    "content": merged,
                    "embedding": embedding_literal,
                    "embedding_model": self.embedding_model,
                    "created_at": datetime.now(UTC),
                },
            )

        self._run_retention_maintenance_if_due()

    @staticmethod
    def build_system_context(memories: list[LongTermMemoryItem]) -> str:
        if not memories:
            return ""

        lines = [
            "Use relevant long-term memory notes below if they help answer the user.",
            "Do not mention these notes explicitly unless user asks about memory.",
        ]
        for idx, item in enumerate(memories, start=1):
            lines.append(f"{idx}. {item.content}")
        return "\n".join(lines)

    def close(self) -> None:
        self.engine.dispose()

    def get_metrics_snapshot(self) -> dict[str, int | bool]:
        with self._state_lock:
            metrics = dict(self._metrics)
            metrics["circuit_is_open"] = bool(
                self._breaker_open_until is not None and datetime.now(UTC) < self._breaker_open_until
            )
        return metrics
