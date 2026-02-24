import asyncio
import os
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import get_db
from app.main import app
from app.models.message import Message
from app.models.session import Session
from app.models.user import User
from app.services.chat_service import chat_service
from app.services.memory_service import memory_service

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "smoke_test.db"
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
        await conn.run_sync(Session.__table__.create)
        await conn.run_sync(Message.__table__.create)

    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False), engine


async def fake_respond(db, user, session_id, user_message):
    await asyncio.sleep(0)
    return "smoke-ok", [], []


async def fake_extract_and_store_facts(db, user_id, user_text, assistant_text):
    await asyncio.sleep(0)
    return None


async def run() -> None:
    session_factory, engine = await init_db()

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    chat_service.respond = fake_respond
    memory_service.extract_and_store_facts = fake_extract_and_store_facts

    with TestClient(app) as client:
        health = client.get("/health")
        ensure(health.status_code == 200, f"health failed: {health.text}")

        register_payload = {"username": "smoke_user", "password": SMOKE_PASSWORD}
        register = client.post("/api/v1/auth/register", json=register_payload)
        ensure(register.status_code == 200, f"register failed: {register.text}")
        tokens = register.json()

        login = client.post("/api/v1/auth/login", json=register_payload)
        ensure(login.status_code == 200, f"login failed: {login.text}")

        access_token = tokens["access_token"]
        headers = {"Authorization": f"Bearer {access_token}"}

        soul_setup_payload = {
            "user_description": "Я разработчик backend и автоматизирую процессы",
            "assistant_name": "SOUL",
            "emoji": "🧠",
            "style": "direct",
            "tone_modifier": "Прямой, без воды",
            "task_mode": "coding",
        }
        soul_setup = client.post("/api/v1/users/me/soul/setup", json=soul_setup_payload, headers=headers)
        ensure(soul_setup.status_code == 200, f"soul setup failed: {soul_setup.text}")

        chat = client.post("/api/v1/chat", json={"message": "Привет"}, headers=headers)
        ensure(chat.status_code == 200, f"chat failed: {chat.text}")
        body = chat.json()
        ensure(body.get("response") == "smoke-ok", f"unexpected response: {body}")

    await engine.dispose()
    print("SMOKE_OK")

    if DB_PATH.exists():
        DB_PATH.unlink()


if __name__ == "__main__":
    asyncio.run(run())
