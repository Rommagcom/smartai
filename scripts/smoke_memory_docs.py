import asyncio
import os
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import get_db
from app.main import app
from app.models.long_term_memory import LongTermMemory
from app.models.user import User
from app.services.memory_service import memory_service
from app.services.rag_service import rag_service

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "smoke_memory_docs.db"
SMOKE_PASSWORD = os.getenv("SMOKE_TEST_PASSWORD", "SmokePass123")


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def init_db() -> tuple[async_sessionmaker[AsyncSession], object]:
    if DB_PATH.exists():
        DB_PATH.unlink()

    engine = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(User.__table__.create)
        await conn.run_sync(LongTermMemory.__table__.create)

    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False), engine


async def run() -> None:
    session_factory, engine = await init_db()

    async def override_get_db():
        async with session_factory() as session:
            yield session

    async def fake_create_long_term_memory(db, user_id, fact_type, content, importance_score=0.5, expiration_date=None):
        memory = LongTermMemory(
            id=uuid.uuid4(),
            user_id=user_id,
            fact_type=fact_type,
            content=content,
            embedding=[0.0] * 1024,
            importance_score=importance_score,
            expiration_date=expiration_date,
        )
        db.add(memory)
        await db.flush()
        return memory

    async def fake_ingest_document(user_id: str, filename: str, content: bytes) -> int:
        await asyncio.sleep(0)
        return 3

    async def fake_retrieve_context(user_id: str, query: str, top_k: int = 5) -> list[dict]:
        await asyncio.sleep(0)
        return [
            {
                "score": 0.01,
                "chunk_text": "Smoke chunk",
                "source_doc": "smoke.txt",
                "metadata": {"user_id": user_id},
            }
        ]

    app.dependency_overrides[get_db] = override_get_db
    memory_service.create_long_term_memory = fake_create_long_term_memory
    rag_service.ingest_document = fake_ingest_document
    rag_service.retrieve_context = fake_retrieve_context

    with TestClient(app) as client:
        credentials = {"username": "memdoc_user", "password": SMOKE_PASSWORD}
        register = client.post("/api/v1/auth/register", json=credentials)
        ensure(register.status_code == 200, f"register failed: {register.text}")

        token = register.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        memory_payload = {
            "fact_type": "preference",
            "content": "Любит краткие ответы",
            "importance_score": 0.8,
            "expiration_date": None,
        }

        create_memory = client.post("/api/v1/memory", json=memory_payload, headers=headers)
        ensure(create_memory.status_code == 200, f"create memory failed: {create_memory.text}")

        list_memory = client.get("/api/v1/memory", headers=headers)
        ensure(list_memory.status_code == 200, f"list memory failed: {list_memory.text}")
        items = list_memory.json()
        ensure(len(items) >= 1, f"memory list empty: {list_memory.text}")

        upload = client.post(
            "/api/v1/documents/upload",
            headers=headers,
            files={"file": ("smoke.txt", b"doc text", "text/plain")},
        )
        ensure(upload.status_code == 200, f"upload failed: {upload.text}")
        ensure(upload.json().get("chunks") == 3, f"unexpected chunks: {upload.text}")

        search = client.get("/api/v1/documents/search", params={"query": "doc", "top_k": 3}, headers=headers)
        ensure(search.status_code == 200, f"search failed: {search.text}")
        ensure(len(search.json().get("items", [])) == 1, f"unexpected search items: {search.text}")

    await engine.dispose()
    if DB_PATH.exists():
        DB_PATH.unlink()

    print("SMOKE_MEMORY_DOCS_OK")


if __name__ == "__main__":
    asyncio.run(run())
