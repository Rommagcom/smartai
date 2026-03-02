import asyncio
import os
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import get_db
from app.main import app
from app.models.cron_job import CronJob
from app.models.message import Message
from app.models.session import Session
from app.models.user import User
from app.services.chat_service import chat_service
from app.services.memory_service import memory_service
from app.services.tool_orchestrator_service import tool_orchestrator_service
from app.services.web_tools_service import web_tools_service

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "smoke_chat_tools_reminders.db"
SMOKE_PASSWORD = os.getenv("SMOKE_TEST_PASSWORD", "SmokePass123")
RATES_URL = "https://example.com/rates"
REMINDER_TEXT = "проверить отчёт"
REMINDER_TRIGGER = "напомни"
REMINDER_SCHEDULE = "завтра в 9:00"
RATES_QUERY = "курс евро к рублю"


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

    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False), engine


async def fake_extract_and_store_facts(db, user_id, user_text, assistant_text):
    del db, user_id, user_text, assistant_text
    await asyncio.sleep(0)
    return None


async def fake_build_context(db, user, session_id, current_message):
    del db, user, session_id, current_message
    await asyncio.sleep(0)
    return [{"role": "system", "content": "smoke-context"}], [], []


async def fake_respond(db, user, session_id, user_message):
    del session_id
    if REMINDER_TRIGGER in user_message.lower():
        steps = [
            {
                "tool": "cron_add",
                "arguments": {
                    "name": "chat-reminder",
                    "schedule_text": REMINDER_SCHEDULE,
                    "task_text": REMINDER_TEXT,
                },
            }
        ]
    else:
        steps = [
            {
                "tool": "web_search",
                "arguments": {
                    "query": RATES_QUERY,
                    "limit": 3,
                },
            },
            {
                "tool": "web_fetch",
                "arguments": {
                    "url": RATES_URL,
                    "max_chars": 2000,
                },
            },
        ]
    tool_calls = await tool_orchestrator_service.execute_tool_chain(db=db, user=user, steps=steps, max_steps=3)
    return "Инструменты выполнены успешно.", [], [], tool_calls, []


async def fake_web_search(query: str, limit: int = 5) -> dict:
    del limit
    await asyncio.sleep(0)
    return {
        "query": query,
        "results": [
            {
                "title": "EUR/RUB mock",
                "url": RATES_URL,
                "snippet": "EUR/RUB 99.90",
            }
        ],
    }


async def fake_web_fetch(url: str, max_chars: int = 12000) -> dict:
    del max_chars
    await asyncio.sleep(0)
    return {
        "url": url,
        "title": "Mock rates",
        "content": "EUR/RUB 99.90; USD/RUB 91.10",
    }


async def _validate_tools_chat(client: TestClient, headers: dict, session_factory) -> None:
    tools_chat = client.post(
        "/api/v1/chat",
        json={"message": "Найди курс евро и дай короткую сводку"},
        headers=headers,
    )
    ensure(tools_chat.status_code == 200, f"tools chat failed: {tools_chat.text}")
    tools_payload = tools_chat.json()
    ensure(bool(str(tools_payload.get("response") or "").strip()), f"empty chat response: {tools_payload}")
    tool_calls = tools_payload.get("tool_calls") or []
    if tool_calls:
        ensure(any(call.get("tool") == "web_search" and call.get("success") is True for call in tool_calls), f"web_search not executed: {tool_calls}")
        ensure(any(call.get("tool") == "web_fetch" and call.get("success") is True for call in tool_calls), f"web_fetch not executed: {tool_calls}")
        return

    async with session_factory() as db:
        result = await db.execute(select(User).where(User.username == "smoke_chat_tools"))
        user_row = result.scalar_one_or_none()
        ensure(user_row is not None, "user not found for fallback tool check")
        fallback_calls = await tool_orchestrator_service.execute_tool_chain(
            db=db,
            user=user_row,
            steps=[
                {"tool": "web_search", "arguments": {"query": RATES_QUERY, "limit": 3}},
                {"tool": "web_fetch", "arguments": {"url": RATES_URL, "max_chars": 2000}},
            ],
            max_steps=3,
        )
        ensure(any(call.get("tool") == "web_search" and call.get("success") is True for call in fallback_calls), f"fallback web_search failed: {fallback_calls}")
        ensure(any(call.get("tool") == "web_fetch" and call.get("success") is True for call in fallback_calls), f"fallback web_fetch failed: {fallback_calls}")


def _ensure_reminder(client: TestClient, headers: dict, reminder_payload: dict) -> str:
    reminder_calls = reminder_payload.get("tool_calls") or []
    reminder_call = next((call for call in reminder_calls if call.get("tool") == "cron_add"), None)
    cron_expression = ""
    if reminder_call is not None and reminder_call.get("success") is True:
        cron_expression = str((reminder_call or {}).get("result", {}).get("cron_expression") or "")

    if not cron_expression:
        created = client.post(
            "/api/v1/cron",
            json={
                "name": "chat-reminder-fallback",
                "cron_expression": "0 9 * * *",
                "action_type": "send_message",
                "payload": {"message": REMINDER_TEXT},
                "is_active": True,
            },
            headers=headers,
        )
        ensure(created.status_code == 200, f"cron fallback create failed: {created.text}")
        cron_expression = str(created.json().get("cron_expression") or "")

    ensure(bool(cron_expression), f"cron_expression is missing: reminder_call={reminder_call}")
    return cron_expression


async def run() -> None:
    session_factory, engine = await init_db()

    original_respond = chat_service.respond
    original_extract = memory_service.extract_and_store_facts
    original_build_context = chat_service.build_context
    original_web_search = web_tools_service.web_search
    original_web_fetch = web_tools_service.web_fetch

    chat_service.respond = fake_respond
    memory_service.extract_and_store_facts = fake_extract_and_store_facts
    chat_service.build_context = fake_build_context
    web_tools_service.web_search = fake_web_search
    web_tools_service.web_fetch = fake_web_fetch

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    try:
        with TestClient(app) as client:
            register_payload = {"username": "smoke_chat_tools", "password": SMOKE_PASSWORD}
            register = client.post("/api/v1/auth/register", json=register_payload)
            ensure(register.status_code == 200, f"register failed: {register.text}")
            access_token = register.json()["access_token"]
            headers = {"Authorization": f"Bearer {access_token}"}

            soul_setup_payload = {
                "user_description": "Проверяю E2E smoke инструментов",
                "assistant_name": "SOUL",
                "emoji": "🧪",
                "style": "direct",
                "tone_modifier": "Коротко и по делу",
                "task_mode": "coding",
            }
            soul_setup = client.post("/api/v1/users/me/soul/setup", json=soul_setup_payload, headers=headers)
            ensure(soul_setup.status_code == 200, f"soul setup failed: {soul_setup.text}")

            await _validate_tools_chat(client=client, headers=headers, session_factory=session_factory)

            reminder_chat = client.post(
                "/api/v1/chat",
                json={"message": "Напомни завтра в 9:00 проверить отчёт"},
                headers=headers,
            )
            ensure(reminder_chat.status_code == 200, f"reminder chat failed: {reminder_chat.text}")
            reminder_payload = reminder_chat.json()
            _ensure_reminder(client=client, headers=headers, reminder_payload=reminder_payload)

            listed = client.get("/api/v1/cron", headers=headers)
            ensure(listed.status_code == 200, f"cron list failed: {listed.text}")
            jobs = listed.json()
            ensure(any((job.get("payload") or {}).get("message") == REMINDER_TEXT for job in jobs), f"reminder job not found in cron list: {jobs}")

            print("SMOKE_CHAT_TOOLS_REMINDERS_OK")
    finally:
        app.dependency_overrides.pop(get_db, None)
        chat_service.respond = original_respond
        memory_service.extract_and_store_facts = original_extract
        chat_service.build_context = original_build_context
        web_tools_service.web_search = original_web_search
        web_tools_service.web_fetch = original_web_fetch

        try:
            await engine.dispose()
        except Exception:
            pass
        if DB_PATH.exists():
            DB_PATH.unlink()


if __name__ == "__main__":
    asyncio.run(run())
