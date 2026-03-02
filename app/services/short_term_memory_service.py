"""Short-term memory (STM) — Redis-based conversational context that bridges sessions.

Unlike long-term memory (PostgreSQL + pgvector), STM is:
- **Fast**: Redis reads/writes, no embeddings or vector search.
- **Ephemeral**: auto-expires via TTL (default 4 hours).
- **Cross-session**: context carries over between browser reloads / new sessions.

Each entry is a compact summary of a recent conversation exchange.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from redis.asyncio import Redis

from app.core.config import settings

logger = logging.getLogger(__name__)


class ShortTermMemoryService:
    """Redis-backed short-term memory per user."""

    def __init__(self) -> None:
        self._redis: Redis | None = None

    def _get_redis(self) -> Redis:
        if self._redis is None:
            self._redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        return self._redis

    @staticmethod
    def _key(user_id: str | UUID) -> str:
        return f"{settings.STM_REDIS_KEY_PREFIX}:{user_id}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def append(self, user_id: str | UUID, summary: str, *, meta: dict | None = None) -> None:
        """Append a context snippet for *user_id*.

        Keeps at most ``STM_MAX_ITEMS`` entries (FIFO) and refreshes the TTL.
        """
        if not summary or not str(summary).strip():
            return

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "text": str(summary).strip()[:500],  # hard cap per entry
        }
        if meta:
            entry["meta"] = meta

        key = self._key(user_id)
        max_items = max(1, int(settings.STM_MAX_ITEMS))
        ttl = max(60, int(settings.STM_TTL_SECONDS))

        try:
            redis = self._get_redis()
            await redis.rpush(key, json.dumps(entry, ensure_ascii=False))
            await redis.ltrim(key, -max_items, -1)
            await redis.expire(key, ttl)
        except Exception:
            logger.debug("STM append failed (Redis unavailable), skipping", exc_info=True)

    async def get_recent(self, user_id: str | UUID, limit: int = 10) -> list[dict]:
        """Return the most recent *limit* context snippets (oldest→newest)."""
        key = self._key(user_id)
        count = max(1, min(limit, int(settings.STM_MAX_ITEMS)))
        try:
            redis = self._get_redis()
            raw_items = await redis.lrange(key, -count, -1)
            items: list[dict] = []
            for raw in raw_items:
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict) and obj.get("text"):
                        items.append(obj)
                except (TypeError, ValueError):
                    continue
            return items
        except Exception:
            logger.debug("STM read failed (Redis unavailable), returning empty", exc_info=True)
            return []

    async def clear(self, user_id: str | UUID) -> None:
        """Remove all STM entries for a user."""
        try:
            redis = self._get_redis()
            await redis.delete(self._key(user_id))
        except Exception:
            logger.debug("STM clear failed", exc_info=True)

    async def size(self, user_id: str | UUID) -> int:
        """Return how many STM entries exist for the user."""
        try:
            redis = self._get_redis()
            return await redis.llen(self._key(user_id))
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Helpers for chat integration
    # ------------------------------------------------------------------

    def format_for_context(self, items: list[dict], max_lines: int = 8) -> str:
        """Format STM entries as a bullet list suitable for the system prompt."""
        if not items:
            return ""
        lines: list[str] = []
        for item in items[-max_lines:]:
            text = str(item.get("text", "")).strip()
            if text:
                lines.append(f"- {text}")
        return "\n".join(lines)


short_term_memory_service = ShortTermMemoryService()
