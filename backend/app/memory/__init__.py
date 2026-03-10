"""Unified memory manager — bridges short-term, long-term and semantic memory.

Architecture:
  - **Short-term (STM)**: Redis FIFO, current conversation context (24h TTL)
  - **Long-term (LTM)**: PostgreSQL + pgvector, persistent facts with decay
  - **Semantic**: Entity extraction from user messages → profile attributes
  - **Summarization**: Compresses old messages to keep context window lean

This module is a facade over the existing memory_service, short_term_memory_service,
and rag_service — it does NOT replace them, it orchestrates them.
"""
from __future__ import annotations

import logging
import re
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.schemas.graph import ExtractedEntity, MemoryExtractionOutput

logger = logging.getLogger(__name__)


class MemoryManager:
    """Unified memory facade used by LangGraph nodes."""

    # ------------------------------------------------------------------
    # Context gathering (read path)
    # ------------------------------------------------------------------

    async def gather_context(
        self,
        db: AsyncSession,
        user_id: UUID,
        session_id: UUID,
        user_message: str,
        *,
        history_limit: int = 20,
        stm_limit: int = 10,
        ltm_limit: int = 5,
        rag_limit: int = 3,
    ) -> dict:
        """Collect all memory layers in parallel.

        Returns a dict with keys: history_messages, stm_context,
        ltm_context, rag_context, history_summary.
        """
        import asyncio

        history_task = asyncio.create_task(
            self._get_history(db, user_id, session_id, history_limit)
        )
        stm_task = asyncio.create_task(self._get_stm(user_id, stm_limit))
        ltm_task = asyncio.create_task(
            self._get_ltm(db, user_id, user_message, ltm_limit)
        )
        rag_task = asyncio.create_task(
            self._get_rag(user_id, user_message, rag_limit)
        )

        history_messages = await history_task
        stm_context = await stm_task
        ltm_context = await ltm_task
        rag_context = await rag_task

        # Build summary for dropped history
        history_summary = self._summarize_dropped_history(
            history_messages,
            max_kept=settings.CONTEXT_ALWAYS_KEEP_LAST_MESSAGES,
        )

        return {
            "history_messages": history_messages,
            "stm_context": stm_context,
            "ltm_context": ltm_context,
            "rag_context": rag_context,
            "history_summary": history_summary,
        }

    # ------------------------------------------------------------------
    # Entity extraction (semantic memory)
    # ------------------------------------------------------------------

    async def extract_entities(self, user_message: str) -> list[ExtractedEntity]:
        """Extract structured entities from user text for profile storage.

        Example: "Я живу в Москве" → ExtractedEntity(entity_type="location", key="city", value="Москва")
        """
        entities: list[ExtractedEntity] = []

        # Timezone
        tz_match = re.search(
            r"\b(?:utc|gmt)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?\b",
            user_message,
            re.IGNORECASE,
        )
        if tz_match:
            sign = "+" if tz_match.group(1) == "+" else "-"
            hour = int(tz_match.group(2))
            minute = int(tz_match.group(3) or "0")
            if hour <= 14 and minute <= 59:
                entities.append(ExtractedEntity(
                    entity_type="preference",
                    key="timezone",
                    value=f"UTC{sign}{hour:02d}:{minute:02d}",
                    confidence=0.95,
                ))

        # City/location
        city_match = re.search(
            r"\b(?:живу\s+в|из|нахожусь\s+в|мой\s+город|i\s+live\s+in|i\'m\s+from|located\s+in)\s+([А-ЯЁа-яёA-Za-z\s-]{2,30})\b",
            user_message,
            re.IGNORECASE,
        )
        if city_match:
            entities.append(ExtractedEntity(
                entity_type="location",
                key="city",
                value=city_match.group(1).strip(),
                confidence=0.85,
            ))

        # Name
        name_match = re.search(
            r"\b(?:меня\s+зовут|мое\s+имя|my\s+name\s+is|i\'m|я\s+[-—]\s+)\s*([А-ЯЁA-Z][а-яёa-z]{1,20})\b",
            user_message,
            re.IGNORECASE,
        )
        if name_match:
            entities.append(ExtractedEntity(
                entity_type="identity",
                key="name",
                value=name_match.group(1).strip(),
                confidence=0.9,
            ))

        # For more complex extraction, we can optionally call LLM
        if not entities and self._might_contain_entities(user_message):
            entities = await self._llm_extract_entities(user_message)

        return entities

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    async def store_entities(
        self,
        db: AsyncSession,
        user_id: UUID,
        entities: list[ExtractedEntity],
    ) -> int:
        """Store extracted entities as long-term memory facts."""
        from app.services.memory_service import memory_service

        stored = 0
        for entity in entities:
            if entity.confidence < 0.7:
                continue
            content = f"{entity.key}={entity.value}"
            await memory_service.create_long_term_memory(
                db=db,
                user_id=user_id,
                fact_type=entity.entity_type,
                content=content,
                importance_score=entity.confidence,
            )
            stored += 1

        if stored:
            await db.flush()
        return stored

    async def append_stm(
        self,
        user_id: UUID,
        user_message: str,
        assistant_response: str,
    ) -> None:
        """Append exchange summary to short-term memory."""
        from app.services.short_term_memory_service import short_term_memory_service

        summary = self._build_exchange_summary(user_message, assistant_response)
        if summary:
            await short_term_memory_service.append(user_id, summary)

    # ------------------------------------------------------------------
    # Summarization layer
    # ------------------------------------------------------------------

    def _summarize_dropped_history(
        self,
        history: list[dict],
        max_kept: int,
    ) -> str | None:
        """Compress history beyond the kept window into a concise summary."""
        if len(history) <= max_kept:
            return None

        dropped = history[:-max_kept] if max_kept > 0 else history
        if not dropped:
            return None

        lines: list[str] = []
        max_items = settings.CONTEXT_SUMMARY_MAX_ITEMS
        max_chars = settings.CONTEXT_SUMMARY_ITEM_MAX_CHARS

        for msg in dropped[-max_items:]:
            role = "Пользователь" if msg.get("role") == "user" else "Ассистент"
            content = str(msg.get("content") or "")[:max_chars]
            if content:
                lines.append(f"- {role}: {content}")

        if not lines:
            return None

        return (
            "Сжатый контекст предыдущего диалога:\n"
            + "\n".join(lines)
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_history(
        self, db: AsyncSession, user_id: UUID, session_id: UUID, limit: int
    ) -> list[dict]:
        from app.services.memory_service import memory_service

        try:
            messages = await memory_service.get_recent_messages(
                db, user_id, session_id=session_id, limit=limit
            )
            return [
                {"role": str(m.role or "assistant"), "content": str(m.content or "")}
                for m in messages
                if str(m.content or "").strip()
            ]
        except Exception:
            logger.debug("history fetch failed", exc_info=True)
            return []

    async def _get_stm(self, user_id: UUID, limit: int) -> list[str]:
        from app.services.short_term_memory_service import short_term_memory_service

        try:
            items = await short_term_memory_service.get_recent(user_id, limit=limit)
            return [str(item.get("text") or "") for item in items if item.get("text")]
        except Exception:
            logger.debug("STM fetch failed", exc_info=True)
            return []

    async def _get_ltm(
        self, db: AsyncSession, user_id: UUID, query: str, limit: int
    ) -> list[str]:
        from app.services.memory_service import memory_service

        try:
            memories = await memory_service.retrieve_relevant_memories(
                db, user_id, query=query, top_k=limit
            )
            return [str(m.content or "") for m in memories if str(m.content or "").strip()]
        except Exception:
            logger.debug("LTM fetch failed", exc_info=True)
            return []

    async def _get_rag(self, user_id: UUID, query: str, limit: int) -> list[str]:
        from app.services.rag_service import rag_service

        try:
            chunks = await rag_service.retrieve_context(
                user_id=user_id, query=query, top_k=limit
            )
            return [str(c) for c in chunks if str(c).strip()]
        except Exception:
            logger.debug("RAG fetch failed", exc_info=True)
            return []

    @staticmethod
    def _build_exchange_summary(user_msg: str, assistant_msg: str) -> str:
        user_part = (user_msg or "").strip()[:200]
        assistant_part = (assistant_msg or "").strip()[:200]
        if not user_part:
            return ""
        parts = [f"Q: {user_part}"]
        if assistant_part:
            parts.append(f"A: {assistant_part}")
        return " | ".join(parts)

    @staticmethod
    def _might_contain_entities(text: str) -> bool:
        """Quick heuristic: does the text likely contain storable facts?"""
        lowered = text.lower()
        markers = [
            "запомни", "мой ", "моя ", "мое ", "я работаю", "я живу",
            "my ", "i am", "i'm", "i live", "i work",
        ]
        return any(m in lowered for m in markers)

    async def _llm_extract_entities(self, text: str) -> list[ExtractedEntity]:
        """Use LLM to extract entities for complex cases."""
        try:
            from app.llm import llm_provider

            result = await llm_provider.chat_structured(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Extract factual entities (name, location, preferences, etc.) "
                            "from the user message. Return only high-confidence extractions."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                response_model=MemoryExtractionOutput,
                temperature=0.0,
            )
            return result.entities if result.should_store else []
        except Exception:
            logger.debug("LLM entity extraction failed", exc_info=True)
            return []


# Singleton
memory_manager = MemoryManager()
