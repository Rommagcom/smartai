import asyncio
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

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "smoke_chat_cron_add.db"


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


async def run() -> None:
    session_factory, engine = await init_db()

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as client:
        credentials = {"username": "chat_cron_user", "password": "SmokePass123"}
        register = client.post("/api/v1/auth/register", json=credentials)
        ensure(register.status_code == 200, f"register failed: {register.text}")
        token = register.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        message_one = (
            "Запланируй напоминание через 5 минут что мне нужно идти домой\n"
            "```cron_add\n"
            "time: in 5 minutes\n"
            "message: Пора идти домой 🏠\n"
            "```"
        )
        message_two = (
            "Создай напоминание через 5 минут нужно домой\n"
            "```cron_add\n"
            "time: in 5 minutes\n"
            "message: Нужно домой 🏠\n"
            "```"
        )
        message_three = "Напомни через 5 минут выключить плиту"
        message_invalid = (
            "Запланируй напоминание\n"
            "```cron_add\n"
            "time: sometime later\n"
            "```"
        )

        response_one = client.post("/api/v1/chat", json={"message": message_one}, headers=headers)
        ensure(response_one.status_code == 200, f"chat #1 failed: {response_one.text}")
        body_one = response_one.json()
        calls_one = body_one.get("tool_calls") if isinstance(body_one.get("tool_calls"), list) else []
        ensure(any(str(c.get("tool") or "") == "cron_add" and bool(c.get("success")) for c in calls_one), f"cron_add not executed in chat #1: {body_one}")

        response_two = client.post("/api/v1/chat", json={"message": message_two}, headers=headers)
        ensure(response_two.status_code == 200, f"chat #2 failed: {response_two.text}")
        body_two = response_two.json()
        calls_two = body_two.get("tool_calls") if isinstance(body_two.get("tool_calls"), list) else []
        ensure(any(str(c.get("tool") or "") == "cron_add" and bool(c.get("success")) for c in calls_two), f"cron_add not executed in chat #2: {body_two}")

        response_three = client.post("/api/v1/chat", json={"message": message_three}, headers=headers)
        ensure(response_three.status_code == 200, f"chat #3 failed: {response_three.text}")
        body_three = response_three.json()
        calls_three = body_three.get("tool_calls") if isinstance(body_three.get("tool_calls"), list) else []
        ensure(any(str(c.get("tool") or "") == "cron_add" and bool(c.get("success")) for c in calls_three), f"cron_add not executed in chat #3: {body_three}")

        listed = client.get("/api/v1/cron", headers=headers)
        ensure(listed.status_code == 200, f"cron list failed: {listed.text}")
        jobs = listed.json() if isinstance(listed.json(), list) else []
        ensure(len(jobs) >= 3, f"expected at least 3 cron jobs, got {len(jobs)}")

        payload_messages = [
            str((item.get("payload") or {}).get("message") or "")
            for item in jobs
            if isinstance(item, dict)
        ]
        ensure(any("Пора идти домой" in text for text in payload_messages), f"first reminder not found in payloads: {payload_messages}")
        ensure(any("Нужно домой" in text for text in payload_messages), f"second reminder not found in payloads: {payload_messages}")
        ensure(any("выключить плиту" in text for text in payload_messages), f"third reminder not found in payloads: {payload_messages}")

        before_invalid_count = len(jobs)
        invalid_response = client.post("/api/v1/chat", json={"message": message_invalid}, headers=headers)
        ensure(invalid_response.status_code == 200, f"chat invalid failed: {invalid_response.text}")
        invalid_body = invalid_response.json()
        invalid_calls = invalid_body.get("tool_calls") if isinstance(invalid_body.get("tool_calls"), list) else []
        ensure(
            not any(str(c.get("tool") or "") == "cron_add" and bool(c.get("success")) for c in invalid_calls),
            f"invalid cron_add unexpectedly succeeded: {invalid_body}",
        )

        listed_after_invalid = client.get("/api/v1/cron", headers=headers)
        ensure(listed_after_invalid.status_code == 200, f"cron list after invalid failed: {listed_after_invalid.text}")
        jobs_after_invalid = listed_after_invalid.json() if isinstance(listed_after_invalid.json(), list) else []
        ensure(
            len(jobs_after_invalid) == before_invalid_count,
            f"invalid cron_add changed job count: before={before_invalid_count}, after={len(jobs_after_invalid)}",
        )

    async with session_factory() as session:
        result = await session.execute(select(CronJob))
        rows = result.scalars().all()
        ensure(len(rows) >= 3, f"DB check failed: expected >=3 cron rows, got {len(rows)}")

    try:
        await engine.dispose()
    except Exception:
        pass

    if DB_PATH.exists():
        DB_PATH.unlink()

    print("SMOKE_CHAT_CRON_ADD_OK")


if __name__ == "__main__":
    asyncio.run(run())
