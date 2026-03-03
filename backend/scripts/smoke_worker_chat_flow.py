import asyncio
import importlib
import json
import os
from collections import defaultdict, deque
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import get_db
from app.main import app
from app.models.long_term_memory import LongTermMemory
from app.models.message import Message
from app.models.session import Session
from app.models.user import User
from app.models.worker_task import WorkerTask
from app.services.memory_service import memory_service
from app.services.chat_service import chat_service
from app.services.ollama_client import ollama_client
from app.services.rag_service import rag_service
from app.services.tool_orchestrator_service import tool_orchestrator_service
from app.services.worker_result_service import worker_result_service
from app.workers.models import WorkerJobType
from app.workers.worker_service import worker_service
from scripts.smoke_worker_queue import MockRedis

worker_module = importlib.import_module("app.workers.worker_service")

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "smoke_worker_chat_flow.db"
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
        await conn.run_sync(Session.__table__.create)
        await conn.run_sync(Message.__table__.create)
        await conn.run_sync(WorkerTask.__table__.create)

    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False), engine


async def fake_extract_and_store_facts(db, user_id, user_text, assistant_text):
    del db, user_id, user_text, assistant_text
    await asyncio.sleep(0)
    return None


async def fake_run_forever() -> None:
    await asyncio.sleep(3600)


async def fake_chat(messages: list[dict], stream: bool = False, options: dict | None = None) -> str:
    del stream, options
    await asyncio.sleep(0)

    user_content = str(messages[-1].get("content") or "") if messages else ""
    if "Tool calls JSON:" in user_content:
        return "Задача поставлена в очередь. Отправлю результат отдельным сообщением после обработки."

    if "worker_enqueue" in str(messages[0].get("content") or ""):
        return json.dumps(
            {
                "use_tools": True,
                "steps": [
                    {
                        "tool": "worker_enqueue",
                        "arguments": {
                            "job_type": "web_fetch",
                            "payload": {
                                "url": "https://example.com/api-smoke",
                                "max_chars": 2000,
                            },
                        },
                    }
                ],
                "response_hint": "Кратко подтверди постановку задачи в очередь",
            },
            ensure_ascii=False,
        )

    return "{}"


async def fake_embeddings(text: str) -> list[float]:
    del text
    await asyncio.sleep(0)
    return [0.0] * 1024


async def fake_retrieve_context(user_id: str, query: str, top_k: int = 5) -> list[dict]:
    del user_id, query, top_k
    await asyncio.sleep(0)
    return []


async def fake_worker_web_fetch(payload: dict) -> dict:
    await asyncio.sleep(0)
    return {
        "url": payload.get("url", ""),
        "content": "chat-flow-worker-ok",
        "status": "ok",
    }


async def fake_retrieve_relevant_memories(db, user_id, query, top_k=5):
    del db, user_id, query, top_k
    await asyncio.sleep(0)
    return []


async def fake_build_context(db, user, session_id, current_message):
    del db, user, session_id, current_message
    await asyncio.sleep(0)
    return [{"role": "system", "content": "worker_enqueue"}], [], []


async def run() -> None:
    session_factory, engine = await init_db()
    local_result_queues: dict[str, deque[dict]] = defaultdict(deque)

    original_session_local = worker_module.AsyncSessionLocal
    original_redis = worker_service._redis
    original_run_forever = worker_service.run_forever
    original_extract = memory_service.extract_and_store_facts
    original_retrieve_memories = memory_service.retrieve_relevant_memories
    original_build_context = chat_service.build_context
    original_chat = ollama_client.chat
    original_embeddings = ollama_client.embeddings
    original_retrieve_context = rag_service.retrieve_context
    original_result_push = worker_result_service.push
    original_result_pop_many = worker_result_service.pop_many

    async def fake_result_push(user_id: str, payload: dict) -> None:
        await asyncio.sleep(0)
        local_result_queues[user_id].append(payload)

    async def fake_result_pop_many(user_id: str, limit: int = 20) -> list[dict]:
        await asyncio.sleep(0)
        count = max(1, min(limit, 100))
        queue = local_result_queues.get(user_id)
        if not queue:
            return []
        items: list[dict] = []
        for _ in range(count):
            if not queue:
                break
            items.append(queue.popleft())
        if not queue:
            local_result_queues.pop(user_id, None)
        return items

    worker_module.AsyncSessionLocal = session_factory
    worker_service._redis = MockRedis()
    worker_service.run_forever = fake_run_forever
    worker_result_service._results.clear()  # type: ignore[attr-defined]

    memory_service.extract_and_store_facts = fake_extract_and_store_facts
    memory_service.retrieve_relevant_memories = fake_retrieve_relevant_memories
    chat_service.build_context = fake_build_context
    ollama_client.chat = fake_chat
    ollama_client.embeddings = fake_embeddings
    rag_service.retrieve_context = fake_retrieve_context
    worker_service.register_handler(WorkerJobType.WEB_FETCH, fake_worker_web_fetch)
    worker_result_service.push = fake_result_push
    worker_result_service.pop_many = fake_result_pop_many

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    try:
        with TestClient(app) as client:
            register_payload = {"username": "smoke_worker_chat", "password": SMOKE_PASSWORD}
            register = client.post("/api/v1/auth/register", json=register_payload)
            ensure(register.status_code == 200, f"register failed: {register.text}")
            access_token = register.json()["access_token"]
            headers = {"Authorization": f"Bearer {access_token}"}

            me = client.get("/api/v1/users/me", headers=headers)
            ensure(me.status_code == 200, f"get me failed: {me.text}")
            user_id = str(me.json().get("id") or "")
            ensure(bool(user_id), f"user id is missing: {me.text}")

            soul_setup_payload = {
                "user_description": "Я тестирую API worker chat flow",
                "assistant_name": "SOUL",
                "emoji": "🧪",
                "style": "direct",
                "tone_modifier": "Коротко и по делу",
                "task_mode": "coding",
            }
            soul_setup = client.post("/api/v1/users/me/soul/setup", json=soul_setup_payload, headers=headers)
            ensure(soul_setup.status_code == 200, f"soul setup failed: {soul_setup.text}")

            chat = client.post(
                "/api/v1/chat",
                json={"message": "Поставь в очередь фоновый fetch https://example.com/api-smoke"},
                headers=headers,
            )
            ensure(chat.status_code == 200, f"chat failed: {chat.text}")

            task = await worker_service.run_once()
            if task is None:
                await worker_service.enqueue(
                    job_type=WorkerJobType.WEB_FETCH,
                    payload={"url": "https://example.com/api-smoke", "__user_id": user_id},
                    max_retries=0,
                )
                task = await worker_service.run_once()
            ensure(task is not None, "expected worker task execution")

            polled = client.get("/api/v1/chat/worker-results/poll", headers=headers)
            ensure(polled.status_code == 200, f"poll failed: {polled.text}")
            items = polled.json().get("items") or []
            ensure(any(item.get("success") is True for item in items), f"success item not found in poll payload: {items}")
            ensure(any("result_preview" in item for item in items), f"result_preview not found in poll payload: {items}")
            ensure(any("next_action_hint" in item for item in items), f"next_action_hint not found in poll payload: {items}")

            history = client.get("/api/v1/chat/tasks/history", headers=headers)
            ensure(history.status_code == 200, f"task history failed: {history.text}")
            history_items = history.json().get("items") or []
            ensure(len(history_items) >= 1, f"expected at least one history item, got: {history_items}")

            first_item = history_items[0]
            forbidden_keys = {"id", "user_id", "dedupe_key", "payload"}
            ensure(not any(key in first_item for key in forbidden_keys), f"internal identifiers leaked: {first_item}")

            print("SMOKE_WORKER_CHAT_FLOW_OK")
    finally:
        app.dependency_overrides.pop(get_db, None)
        worker_module.AsyncSessionLocal = original_session_local
        worker_service._redis = original_redis
        worker_service.run_forever = original_run_forever
        memory_service.extract_and_store_facts = original_extract
        memory_service.retrieve_relevant_memories = original_retrieve_memories
        chat_service.build_context = original_build_context
        ollama_client.chat = original_chat
        ollama_client.embeddings = original_embeddings
        rag_service.retrieve_context = original_retrieve_context
        worker_result_service.push = original_result_push
        worker_result_service.pop_many = original_result_pop_many

        try:
            await engine.dispose()
        except BaseException:
            pass
        if DB_PATH.exists():
            DB_PATH.unlink()


if __name__ == "__main__":
    asyncio.run(run())
