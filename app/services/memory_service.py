from datetime import datetime, timedelta, timezone
import hashlib
import math
import re
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.long_term_memory import LongTermMemory
from app.models.message import Message
from app.models.session import Session
from app.services.ollama_client import ollama_client


class MemoryService:
    @staticmethod
    def _normalized_content(content: str) -> str:
        text = re.sub(r"\s+", " ", str(content or "").strip().lower())
        return text

    @staticmethod
    def _dedupe_key(fact_type: str, content: str) -> str:
        normalized = f"{str(fact_type or '').strip().lower()}|{MemoryService._normalized_content(content)}"
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _active_filter(now: datetime):
        return or_(
            LongTermMemory.expiration_date.is_(None),
            LongTermMemory.expiration_date > now,
            LongTermMemory.is_pinned.is_(True),
            LongTermMemory.is_locked.is_(True),
        )

    @staticmethod
    def _effective_importance(memory: LongTermMemory, now: datetime) -> float:
        base = float(memory.importance_score or 0.0)
        if memory.is_pinned or memory.is_locked:
            return base

        half_life_days = max(1, int(settings.MEMORY_DECAY_HALF_LIFE_DAYS))
        min_factor = max(0.0, min(1.0, float(settings.MEMORY_DECAY_MIN_FACTOR)))
        decay_anchor = memory.last_decay_at or memory.created_at or now
        age_days = max(0.0, (now - decay_anchor).total_seconds() / 86400.0)
        if age_days <= 0:
            return base

        factor = math.exp(-math.log(2.0) * (age_days / float(half_life_days)))
        floor = min_factor + (1.0 - min_factor) * factor
        return max(0.0, min(1.0, base * floor))

    @staticmethod
    def _resolve_expiration_date(now: datetime, expiration_date: datetime | None, is_pinned: bool, is_locked: bool) -> datetime | None:
        if expiration_date is not None:
            return expiration_date
        default_ttl_days = int(settings.MEMORY_DEFAULT_TTL_DAYS)
        if default_ttl_days > 0 and not is_pinned and not is_locked:
            return now + timedelta(days=default_ttl_days)
        return None

    async def _find_duplicate_memory(
        self,
        db: AsyncSession,
        user_id: UUID,
        fact_type: str,
        dedupe_key: str,
        now: datetime,
    ) -> LongTermMemory | None:
        existing_result = await db.execute(
            select(LongTermMemory)
            .where(
                LongTermMemory.user_id == user_id,
                LongTermMemory.fact_type == fact_type,
                LongTermMemory.dedupe_key == dedupe_key,
                self._active_filter(now),
            )
            .order_by(LongTermMemory.created_at.desc())
            .limit(1)
        )
        return existing_result.scalar_one_or_none()

    @staticmethod
    def _merge_duplicate_memory(
        memory: LongTermMemory,
        *,
        now: datetime,
        normalized_importance: float,
        expiration_date: datetime | None,
        is_pinned: bool,
        is_locked: bool,
    ) -> None:
        if memory.is_locked:
            return
        memory.importance_score = max(float(memory.importance_score or 0.0), normalized_importance)
        if expiration_date is not None and (memory.expiration_date is None or expiration_date > memory.expiration_date):
            memory.expiration_date = expiration_date
        if is_pinned and not memory.is_pinned:
            memory.is_pinned = True
            memory.pinned_at = now
            memory.expiration_date = None
        if is_locked and not memory.is_locked:
            memory.is_locked = True
            memory.locked_at = now
            memory.is_pinned = True
            memory.pinned_at = memory.pinned_at or now
            memory.expiration_date = None

    async def get_or_create_session(self, db: AsyncSession, user_id: UUID, session_id: UUID | None) -> Session:
        if session_id:
            result = await db.execute(select(Session).where(Session.id == session_id, Session.user_id == user_id))
            existing = result.scalar_one_or_none()
            if existing:
                return existing

        new_session = Session(user_id=user_id, context_window=[], active=True, last_activity=datetime.now(timezone.utc))
        db.add(new_session)
        await db.flush()
        return new_session

    async def append_message(
        self,
        db: AsyncSession,
        user_id: UUID,
        session_id: UUID,
        role: str,
        content: str,
        message_meta: dict | None = None,
    ) -> Message:
        message = Message(
            user_id=user_id,
            session_id=session_id,
            role=role,
            content=content,
            meta=message_meta or {},
        )
        db.add(message)
        await db.flush()
        return message

    async def get_recent_messages(self, db: AsyncSession, user_id: UUID, session_id: UUID, limit: int = 20) -> list[Message]:
        result = await db.execute(
            select(Message)
            .where(Message.user_id == user_id, Message.session_id == session_id)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        return list(reversed(result.scalars().all()))

    async def create_long_term_memory(
        self,
        db: AsyncSession,
        user_id: UUID,
        fact_type: str,
        content: str,
        importance_score: float = 0.5,
        expiration_date: datetime | None = None,
        is_pinned: bool = False,
        is_locked: bool = False,
    ) -> LongTermMemory:
        now = datetime.now(timezone.utc)
        dedupe_key = self._dedupe_key(fact_type=fact_type, content=content)
        normalized_importance = max(0.0, min(1.0, float(importance_score)))

        expiration_date = self._resolve_expiration_date(now, expiration_date, is_pinned, is_locked)
        existing = await self._find_duplicate_memory(db, user_id, fact_type, dedupe_key, now)
        if existing:
            self._merge_duplicate_memory(
                existing,
                now=now,
                normalized_importance=normalized_importance,
                expiration_date=expiration_date,
                is_pinned=is_pinned,
                is_locked=is_locked,
            )
            await db.flush()
            return existing

        vector = await ollama_client.embeddings(content)
        memory = LongTermMemory(
            user_id=user_id,
            fact_type=fact_type,
            content=content,
            embedding=vector,
            importance_score=normalized_importance,
            dedupe_key=dedupe_key,
            expiration_date=expiration_date,
            is_pinned=is_pinned,
            is_locked=is_locked,
            pinned_at=now if is_pinned else None,
            locked_at=now if is_locked else None,
        )
        db.add(memory)
        await db.flush()
        return memory

    async def retrieve_relevant_memories(self, db: AsyncSession, user_id: UUID, query: str, top_k: int = 5) -> list[LongTermMemory]:
        now = datetime.now(timezone.utc)
        await self.apply_importance_decay(db, user_id)
        try:
            query_embedding = await ollama_client.embeddings(query)
        except Exception:
            return []
        result = await db.execute(
            select(LongTermMemory)
            .where(LongTermMemory.user_id == user_id, self._active_filter(now))
            .order_by(LongTermMemory.embedding.cosine_distance(query_embedding), LongTermMemory.importance_score.desc())
            .limit(max(1, min(top_k * 4, 80)))
        )
        rows = result.scalars().all()
        rows.sort(key=lambda row: self._effective_importance(row, now), reverse=True)
        return rows[: max(1, top_k)]

    async def list_memories(self, db: AsyncSession, user_id: UUID, limit: int = 200) -> list[LongTermMemory]:
        now = datetime.now(timezone.utc)
        await self.apply_importance_decay(db, user_id)
        result = await db.execute(
            select(LongTermMemory)
            .where(LongTermMemory.user_id == user_id, self._active_filter(now))
            .order_by(LongTermMemory.is_pinned.desc(), LongTermMemory.is_locked.desc(), LongTermMemory.importance_score.desc(), LongTermMemory.created_at.desc())
            .limit(max(1, min(limit, 500)))
        )
        return result.scalars().all()

    async def apply_importance_decay(self, db: AsyncSession, user_id: UUID) -> None:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=1)
        result = await db.execute(
            select(LongTermMemory).where(
                LongTermMemory.user_id == user_id,
                LongTermMemory.is_pinned.is_(False),
                LongTermMemory.is_locked.is_(False),
                self._active_filter(now),
                or_(LongTermMemory.last_decay_at.is_(None), LongTermMemory.last_decay_at < cutoff),
            )
        )
        rows = result.scalars().all()
        for row in rows:
            decayed = self._effective_importance(row, now)
            if decayed < float(row.importance_score or 0.0):
                row.importance_score = max(0.0, min(1.0, decayed))
            row.last_decay_at = now
        await db.flush()

    async def set_memory_pin(self, db: AsyncSession, user_id: UUID, memory_id: UUID, value: bool) -> LongTermMemory | None:
        result = await db.execute(select(LongTermMemory).where(LongTermMemory.id == memory_id, LongTermMemory.user_id == user_id))
        memory = result.scalar_one_or_none()
        if not memory:
            return None

        now = datetime.now(timezone.utc)
        memory.is_pinned = bool(value)
        memory.pinned_at = now if value else None
        if value:
            memory.expiration_date = None
        await db.flush()
        return memory

    async def set_memory_lock(self, db: AsyncSession, user_id: UUID, memory_id: UUID, value: bool) -> LongTermMemory | None:
        result = await db.execute(select(LongTermMemory).where(LongTermMemory.id == memory_id, LongTermMemory.user_id == user_id))
        memory = result.scalar_one_or_none()
        if not memory:
            return None

        now = datetime.now(timezone.utc)
        memory.is_locked = bool(value)
        memory.locked_at = now if value else None
        if value:
            memory.is_pinned = True
            memory.pinned_at = memory.pinned_at or now
            memory.expiration_date = None
        await db.flush()
        return memory

    async def cleanup_expired_memories(self, db: AsyncSession, user_id: UUID, limit: int = 500) -> int:
        now = datetime.now(timezone.utc)
        result = await db.execute(
            select(LongTermMemory)
            .where(
                LongTermMemory.user_id == user_id,
                LongTermMemory.is_pinned.is_(False),
                LongTermMemory.is_locked.is_(False),
                LongTermMemory.expiration_date.is_not(None),
                LongTermMemory.expiration_date <= now,
            )
            .limit(max(1, min(limit, 5000)))
        )
        rows = result.scalars().all()
        for row in rows:
            await db.delete(row)
        await db.flush()
        return len(rows)

    async def extract_and_store_facts(self, db: AsyncSession, user_id: UUID, user_text: str, assistant_text: str) -> None:
        prompt = (
            "Извлеки до 3 устойчивых фактов о пользователе из диалога. "
            "Формат ответа строго по строкам: fact_type|content|importance(0..1). "
            "fact_type только: preference, fact, goal, constraint."
        )
        response = await ollama_client.chat(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"User: {user_text}\nAssistant: {assistant_text}"},
            ],
            stream=False,
        )
        for line in response.splitlines():
            parts = [part.strip() for part in line.split("|")]
            if len(parts) != 3:
                continue
            fact_type, content, importance_raw = parts
            if fact_type not in {"preference", "fact", "goal", "constraint"} or not content:
                continue
            try:
                importance = max(0.0, min(1.0, float(importance_raw)))
            except ValueError:
                importance = 0.5
            await self.create_long_term_memory(
                db=db,
                user_id=user_id,
                fact_type=fact_type,
                content=content,
                importance_score=importance,
            )


memory_service = MemoryService()
