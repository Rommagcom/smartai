from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
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
    ) -> None:
        self.database_url = database_url
        self.ollama_base_url = ollama_base_url.rstrip("/")
        self.embedding_model = self._normalize_embedding_model(embedding_model)
        self.api_key = (api_key or "").strip() or None
        self.top_k = max(1, top_k)
        self.max_entry_chars = max(256, max_entry_chars)

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

    def _embed_text(self, text_value: str) -> list[float]:
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

        try:
            with urllib_request.urlopen(req, timeout=20) as response:
                body = response.read().decode("utf-8")
        except urllib_error.URLError as exc:
            raise RuntimeError(f"embedding request failed: {exc}") from exc

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("embedding response is not valid JSON") from exc

        embedding = parsed.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            raise RuntimeError("embedding response does not contain vector")

        normalized: list[float] = []
        for item in embedding:
            normalized.append(float(item))
        return normalized

    def recall(
        self,
        *,
        org_id: str,
        team_id: str,
        user_id: int,
        chat_id: int,
        query_text: str,
        limit: int | None = None,
    ) -> list[LongTermMemoryItem]:
        normalized_query = query_text.strip()
        if not normalized_query:
            return []

        query_embedding = self._embed_text(normalized_query)
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
                            AND team_id = :team_id
                            AND user_id = :user_id
                            AND chat_id = :chat_id
            ORDER BY embedding <=> CAST(:query_vector AS vector)
            LIMIT :row_limit
            """
        )

        items: list[LongTermMemoryItem] = []
        with self.engine.connect() as conn:
            rows = conn.execute(
                stmt,
                {
                    "org_id": org_id,
                    "team_id": team_id,
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

        return items

    def remember(
        self,
        *,
        org_id: str,
        team_id: str,
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

        embedding = self._embed_text(merged)
        embedding_literal = self._vector_literal(embedding)

        stmt = text(
            """
            INSERT INTO long_term_memories (
                org_id, team_id, user_id, chat_id, source, content, embedding, embedding_model, created_at
            )
            VALUES (
                :org_id, :team_id, :user_id, :chat_id, :source, :content, CAST(:embedding AS vector),
                :embedding_model, :created_at
            )
            """
        )

        with self.engine.begin() as conn:
            conn.execute(
                stmt,
                {
                    "org_id": org_id,
                    "team_id": team_id,
                    "user_id": int(user_id),
                    "chat_id": int(chat_id),
                    "source": source,
                    "content": merged,
                    "embedding": embedding_literal,
                    "embedding_model": self.embedding_model,
                    "created_at": datetime.now(UTC),
                },
            )

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
