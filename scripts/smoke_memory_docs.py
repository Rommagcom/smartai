import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import get_db
from app.main import app
from app.models.long_term_memory import LongTermMemory
from app.models.user import User
from app.services.memory_service import memory_service
from app.services.ollama_client import ollama_client
from app.services.rag_service import rag_service

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "smoke_memory_docs.db"
SMOKE_PASSWORD = os.getenv("SMOKE_TEST_PASSWORD", "SmokePass123")
MEMORY_ENDPOINT = "/api/v1/memory"
MEMORY_CLEANUP_ENDPOINT = "/api/v1/memory/cleanup"
SMOKE_DOC_FILENAME = "smoke.txt"


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

    async def fake_embeddings(text: str) -> list[float]:
        del text
        await asyncio.sleep(0)
        return [0.0] * 1024

    async def fake_ingest_document(user_id: str, filename: str, content: bytes) -> int:
        await asyncio.sleep(0)
        return 3

    async def fake_retrieve_context(user_id: str, query: str, top_k: int = 5) -> list[dict]:
        await asyncio.sleep(0)
        return [
            {
                "score": 0.01,
                "chunk_text": "Smoke chunk",
                "source_doc": SMOKE_DOC_FILENAME,
                "metadata": {"user_id": user_id},
            }
        ]

    async def fake_ingest_unavailable(user_id: str, filename: str, content: bytes) -> int:
        del user_id, filename, content
        await asyncio.sleep(0)
        raise RuntimeError("Document embedding is temporarily unavailable")

    async def fake_search_unavailable(user_id: str, query: str, top_k: int = 5) -> list[dict]:
        del user_id, query, top_k
        await asyncio.sleep(0)
        raise RuntimeError("Document search embedding is temporarily unavailable")

    app.dependency_overrides[get_db] = override_get_db
    ollama_client.embeddings = fake_embeddings
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

        create_memory = client.post(MEMORY_ENDPOINT, json=memory_payload, headers=headers)
        ensure(create_memory.status_code == 200, f"create memory failed: {create_memory.text}")
        memory_id = create_memory.json().get("id")
        ensure(bool(memory_id), f"memory id is missing: {create_memory.text}")

        create_memory_duplicate = client.post(MEMORY_ENDPOINT, json=memory_payload, headers=headers)
        ensure(create_memory_duplicate.status_code == 200, f"create duplicate memory failed: {create_memory_duplicate.text}")

        expired_payload = {
            "fact_type": "fact",
            "content": "Устаревший факт",
            "importance_score": 0.4,
            "expiration_date": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            "is_pinned": False,
            "is_locked": False,
        }
        create_expired = client.post(MEMORY_ENDPOINT, json=expired_payload, headers=headers)
        ensure(create_expired.status_code == 200, f"create expired memory failed: {create_expired.text}")

        pin = client.patch(f"/api/v1/memory/{memory_id}/pin", json={"value": True}, headers=headers)
        ensure(pin.status_code == 200, f"pin memory failed: {pin.text}")
        ensure(pin.json().get("is_pinned") is True, f"memory should be pinned: {pin.text}")

        lock = client.patch(f"/api/v1/memory/{memory_id}/lock", json={"value": True}, headers=headers)
        ensure(lock.status_code == 200, f"lock memory failed: {lock.text}")
        ensure(lock.json().get("is_locked") is True, f"memory should be locked: {lock.text}")

        list_memory = client.get(MEMORY_ENDPOINT, headers=headers)
        ensure(list_memory.status_code == 200, f"list memory failed: {list_memory.text}")
        items = list_memory.json()
        ensure(len(items) == 1, f"dedup expected one memory item: {list_memory.text}")

        cleanup = client.post(MEMORY_CLEANUP_ENDPOINT, headers=headers)
        ensure(cleanup.status_code == 200, f"memory cleanup failed: {cleanup.text}")
        ensure(int(cleanup.json().get("deleted_count") or 0) >= 1, f"cleanup should remove expired memory: {cleanup.text}")

        upload = client.post(
            "/api/v1/documents/upload",
            headers=headers,
            files={"file": (SMOKE_DOC_FILENAME, b"doc text", "text/plain")},
        )
        ensure(upload.status_code == 200, f"upload failed: {upload.text}")
        ensure(upload.json().get("chunks") == 3, f"unexpected chunks: {upload.text}")

        search = client.get("/api/v1/documents/search", params={"query": "doc", "top_k": 3}, headers=headers)
        ensure(search.status_code == 200, f"search failed: {search.text}")
        ensure(len(search.json().get("items", [])) == 1, f"unexpected search items: {search.text}")

        rag_service.ingest_document = fake_ingest_unavailable
        upload_503 = client.post(
            "/api/v1/documents/upload",
            headers=headers,
            files={"file": (SMOKE_DOC_FILENAME, b"doc text", "text/plain")},
        )
        ensure(upload_503.status_code == 503, f"upload 503 expected: {upload_503.text}")
        ensure(
            "temporarily unavailable" in str(upload_503.json().get("detail") or "").lower(),
            f"upload 503 detail mismatch: {upload_503.text}",
        )

        rag_service.retrieve_context = fake_search_unavailable
        search_503 = client.get("/api/v1/documents/search", params={"query": "doc", "top_k": 3}, headers=headers)
        ensure(search_503.status_code == 503, f"search 503 expected: {search_503.text}")
        ensure(
            "temporarily unavailable" in str(search_503.json().get("detail") or "").lower(),
            f"search 503 detail mismatch: {search_503.text}",
        )

    try:
        await engine.dispose()
    except Exception:
        pass
    if DB_PATH.exists():
        DB_PATH.unlink()

    print("SMOKE_MEMORY_DOCS_OK")


if __name__ == "__main__":
    asyncio.run(run())
