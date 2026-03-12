import asyncio
from pathlib import Path
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.memory import memory_manager
from app.core.security import get_password_hash
from app.models.long_term_memory import LongTermMemory
from app.models.user import User
from app.services.memory_service import memory_service
from app.services.ollama_client import ollama_client
from app.services.short_term_memory_service import short_term_memory_service
from scripts.smoke_env import apply_smoke_env_defaults, smoke_user_uuid

apply_smoke_env_defaults()

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH_PREFIX = "smoke_memory_context_priority"


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def init_db(db_path: Path) -> tuple[async_sessionmaker[AsyncSession], object]:
    if db_path.exists():
        db_path.unlink()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(User.__table__.create)
        await conn.run_sync(LongTermMemory.__table__.create)

    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False), engine


async def run() -> None:
    db_path = BASE_DIR / f"{DB_PATH_PREFIX}_{uuid4().hex}.db"
    session_factory, engine = await init_db(db_path)

    original_embeddings = ollama_client.embeddings
    original_get_recent = short_term_memory_service.get_recent

    async def fake_embeddings(text: str) -> list[float]:
        del text
        await asyncio.sleep(0)
        return [0.0] * 1024

    async def fake_get_recent(user_id, limit=10):
        del user_id, limit
        await asyncio.sleep(0)
        return [
            {"text": "Q: Привет | A: Здравствуйте"},
            {"text": "Q: Привет | A: Здравствуйте"},
            {"text": "Q: Мой город Алматы | A: Принял"},
        ]

    ollama_client.embeddings = fake_embeddings
    short_term_memory_service.get_recent = fake_get_recent

    try:
        smoke_user_id = smoke_user_uuid()
        async with session_factory() as session:
            user = User(
                id=smoke_user_id,
                username="memory_ctx_user",
                hashed_password=get_password_hash("SmokePass123"),
                preferences={},
                is_admin=False,
            )
            session.add(user)
            await session.flush()

            await memory_service.create_long_term_memory(
                db=session,
                user_id=user.id,
                fact_type="identity",
                content="имя=Роман",
                importance_score=0.7,
            )
            await memory_service.create_long_term_memory(
                db=session,
                user_id=user.id,
                fact_type="preference",
                content="timezone=UTC+03:00",
                importance_score=0.4,
                is_pinned=True,
            )
            await memory_service.create_long_term_memory(
                db=session,
                user_id=user.id,
                fact_type="fact",
                content="любит кататься на велосипеде",
                importance_score=0.95,
            )
            await session.commit()

            selected = await memory_service.retrieve_chat_context_memories(
                db=session,
                user_id=user.id,
                query="как меня зовут и какой у меня часовой пояс",
                top_k=3,
            )
            selected_texts = [str(item.content or "") for item in selected]
            joined = "\n".join(selected_texts)
            ensure("имя=Роман" in joined, f"identity fact is missing: {selected_texts}")
            ensure("timezone=UTC+03:00" in joined, f"pinned preference is missing: {selected_texts}")

            stm_items = await memory_manager._get_stm(user.id, limit=10)
            ensure(len(stm_items) == 2, f"expected STM dedupe to keep 2 items, got: {stm_items}")

        print("SMOKE_MEMORY_CONTEXT_PRIORITY_OK")
    finally:
        ollama_client.embeddings = original_embeddings
        short_term_memory_service.get_recent = original_get_recent
        try:
            await engine.dispose()
        finally:
            if db_path.exists():
                db_path.unlink()


if __name__ == "__main__":
    asyncio.run(run())
