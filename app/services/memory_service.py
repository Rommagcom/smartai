from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.long_term_memory import LongTermMemory
from app.models.message import Message
from app.models.session import Session
from app.services.ollama_client import ollama_client


class MemoryService:
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
    ) -> LongTermMemory:
        vector = await ollama_client.embeddings(content)
        memory = LongTermMemory(
            user_id=user_id,
            fact_type=fact_type,
            content=content,
            embedding=vector,
            importance_score=importance_score,
            expiration_date=expiration_date,
        )
        db.add(memory)
        await db.flush()
        return memory

    async def retrieve_relevant_memories(self, db: AsyncSession, user_id: UUID, query: str, top_k: int = 5) -> list[LongTermMemory]:
        query_embedding = await ollama_client.embeddings(query)
        result = await db.execute(
            select(LongTermMemory)
            .where(LongTermMemory.user_id == user_id)
            .order_by(LongTermMemory.embedding.cosine_distance(query_embedding), LongTermMemory.importance_score.desc())
            .limit(top_k)
        )
        return result.scalars().all()

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
