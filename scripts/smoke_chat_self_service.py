import asyncio
import os
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import get_db
from app.main import app
from app.models.api_integration import ApiIntegration
from app.models.cron_job import CronJob
from app.models.message import Message
from app.models.session import Session
from app.models.user import User
from app.services.api_executor import api_executor
from app.services.chat_service import chat_service
from app.services.memory_service import memory_service
from app.services.tool_orchestrator_service import tool_orchestrator_service
from app.services.web_tools_service import web_tools_service

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / f"smoke_chat_self_service_{os.getpid()}.db"
SMOKE_PASSWORD = os.getenv("SMOKE_TEST_PASSWORD", "SmokePass123")
SMOKE_INTEGRATION_TOKEN = os.getenv("SMOKE_INTEGRATION_TOKEN", "smoke-integration-token")
CHAT_ENDPOINT = "/api/v1/chat"


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def init_db() -> tuple[async_sessionmaker[AsyncSession], object]:
    if DB_PATH.exists():
        DB_PATH.unlink()

    engine = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(User.__table__.create)
        await conn.run_sync(Session.__table__.create)
        await conn.run_sync(Message.__table__.create)
        await conn.run_sync(CronJob.__table__.create)
        await conn.run_sync(ApiIntegration.__table__.create)

    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False), engine


async def fake_extract_and_store_facts(db, user_id, user_text, assistant_text):
    del db, user_id, user_text, assistant_text
    await asyncio.sleep(0)
    return None


async def fake_build_context(db, user, session_id, current_message):
    del db, user, session_id, current_message
    await asyncio.sleep(0)
    return [{"role": "system", "content": "self-service-smoke"}], [], []


async def fake_web_search(query: str, limit: int = 5) -> dict:
    del limit
    await asyncio.sleep(0)
    return {
        "query": query,
        "results": [
            {
                "title": "Mock EUR/RUB",
                "url": "https://example.com/rates",
                "snippet": "EUR/RUB 100.00",
            }
        ],
    }


async def fake_web_fetch(url: str, max_chars: int = 12000) -> dict:
    del max_chars
    await asyncio.sleep(0)
    return {
        "url": url,
        "title": "Mock rates page",
        "content": "EUR/RUB 100.00",
    }


async def fake_integration_call(method: str, url: str, headers: dict | None = None, body: dict | None = None) -> dict:
    await asyncio.sleep(0)
    return {
        "status_code": 200,
        "method": method,
        "url": url,
        "headers": headers or {},
        "body": body or {},
    }


async def fake_respond(db, user, session_id, user_message):
    del session_id
    lowered = str(user_message or "").lower()

    if "курс" in lowered:
        steps = [
            {"tool": "web_search", "arguments": {"query": "курс евро к рублю", "limit": 3}},
            {"tool": "web_fetch", "arguments": {"url": "https://example.com/rates", "max_chars": 2000}},
        ]
    elif "подключи" in lowered and "api" in lowered:
        steps = [
            {
                "tool": "integration_add",
                "arguments": {
                    "service_name": "self_service_api",
                    "token": SMOKE_INTEGRATION_TOKEN,
                    "base_url": "https://example.test",
                    "endpoints": [{"name": "status", "url": "https://example.test/status", "method": "GET"}],
                },
            }
        ]
    elif "проверь" in lowered and "интеграц" in lowered:
        result = await db.execute(
            select(ApiIntegration)
            .where(ApiIntegration.user_id == user.id)
            .order_by(ApiIntegration.created_at.desc())
            .limit(1)
        )
        integration = result.scalar_one_or_none()
        if not integration:
            raise RuntimeError("integration not found for self-service smoke")
        steps = [
            {
                "tool": "integration_call",
                "arguments": {
                    "integration_id": str(integration.id),
                    "url": "https://example.test/status",
                    "method": "GET",
                },
            }
        ]
    elif "напомни" in lowered:
        steps = [
            {
                "tool": "cron_add",
                "arguments": {
                    "name": "self-service-reminder",
                    "cron_expression": "0 9 * * *",
                    "task_text": "проверить отчёт",
                },
            }
        ]
    else:
        steps = []

    tool_calls = await tool_orchestrator_service.execute_tool_chain(db=db, user=user, steps=steps, max_steps=3)
    return "self-service-ok", [], [], tool_calls, []


async def run() -> None:
    session_factory, engine = await init_db()

    original_respond = chat_service.respond
    original_extract = memory_service.extract_and_store_facts
    original_build_context = chat_service.build_context
    original_web_search = web_tools_service.web_search
    original_web_fetch = web_tools_service.web_fetch
    original_api_call = api_executor.call

    chat_service.respond = fake_respond
    memory_service.extract_and_store_facts = fake_extract_and_store_facts
    chat_service.build_context = fake_build_context
    web_tools_service.web_search = fake_web_search
    web_tools_service.web_fetch = fake_web_fetch
    api_executor.call = fake_integration_call

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    try:
        with TestClient(app) as client:
            credentials = {"username": "smoke_self_service", "password": SMOKE_PASSWORD}
            register = client.post("/api/v1/auth/register", json=credentials)
            ensure(register.status_code == 200, f"register failed: {register.text}")
            headers = {"Authorization": f"Bearer {register.json()['access_token']}"}

            tools_chat = client.post(CHAT_ENDPOINT, json={"message": "Найди курс евро"}, headers=headers)
            ensure(tools_chat.status_code == 200, f"tools chat failed: {tools_chat.text}")
            tools_calls = tools_chat.json().get("tool_calls") or []
            ensure(any(call.get("tool") == "web_search" and call.get("success") is True for call in tools_calls), f"web_search missing: {tools_calls}")
            ensure(any(call.get("tool") == "web_fetch" and call.get("success") is True for call in tools_calls), f"web_fetch missing: {tools_calls}")

            me = client.get("/api/v1/users/me", headers=headers)
            ensure(me.status_code == 200, f"get me failed: {me.text}")
            ensure(me.json().get("soul_configured") is True, f"auto setup was not applied: {me.text}")

            add_integration_chat = client.post(
                "/api/v1/chat",
                json={"message": "Подключи API self_service_api"},
                headers=headers,
            )
            ensure(add_integration_chat.status_code == 200, f"integration add via chat failed: {add_integration_chat.text}")
            add_calls = add_integration_chat.json().get("tool_calls") or []
            ensure(any(call.get("tool") == "integration_add" and call.get("success") is True for call in add_calls), f"integration_add missing: {add_calls}")

            check_integration_chat = client.post(
                CHAT_ENDPOINT,
                json={"message": "Проверь интеграцию"},
                headers=headers,
            )
            ensure(check_integration_chat.status_code == 200, f"integration call via chat failed: {check_integration_chat.text}")
            check_calls = check_integration_chat.json().get("tool_calls") or []
            ensure(any(call.get("tool") == "integration_call" and call.get("success") is True for call in check_calls), f"integration_call missing: {check_calls}")

            reminder_chat = client.post(
                CHAT_ENDPOINT,
                json={"message": "Напомни ежедневно в 9 проверить отчёт"},
                headers=headers,
            )
            ensure(reminder_chat.status_code == 200, f"reminder via chat failed: {reminder_chat.text}")
            reminder_calls = reminder_chat.json().get("tool_calls") or []
            ensure(any(call.get("tool") == "cron_add" and call.get("success") is True for call in reminder_calls), f"cron_add missing: {reminder_calls}")

            cron_list = client.get("/api/v1/cron", headers=headers)
            ensure(cron_list.status_code == 200, f"cron list failed: {cron_list.text}")
            jobs = cron_list.json()
            ensure(any((job.get("payload") or {}).get("message") == "проверить отчёт" for job in jobs), f"reminder job not found: {jobs}")

            print("SMOKE_CHAT_SELF_SERVICE_OK")
    finally:
        app.dependency_overrides.pop(get_db, None)
        chat_service.respond = original_respond
        memory_service.extract_and_store_facts = original_extract
        chat_service.build_context = original_build_context
        web_tools_service.web_search = original_web_search
        web_tools_service.web_fetch = original_web_fetch
        api_executor.call = original_api_call

        try:
            await engine.dispose()
        except Exception:
            pass
        if DB_PATH.exists():
            DB_PATH.unlink()


if __name__ == "__main__":
    asyncio.run(run())
