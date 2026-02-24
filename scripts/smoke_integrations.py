import asyncio
import os
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import get_db
from app.main import app
from app.models.api_integration import ApiIntegration
from app.models.telegram_allowed_user import TelegramAllowedUser
from app.models.user import User
from app.services.api_executor import api_executor

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "smoke_integrations.db"
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
        await conn.run_sync(ApiIntegration.__table__.create)
        await conn.run_sync(TelegramAllowedUser.__table__.create)

    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False), engine


async def run() -> None:
    session_factory, engine = await init_db()

    async def override_get_db():
        async with session_factory() as session:
            yield session

    async def fake_call(method: str, url: str, headers: dict | None = None, body: dict | None = None) -> dict:
        await asyncio.sleep(0)
        return {
            "status_code": 200,
            "headers": headers or {},
            "body": f"mocked {method} {url}",
            "echo": body or {},
        }

    app.dependency_overrides[get_db] = override_get_db
    api_executor.call = fake_call

    with TestClient(app) as client:
        credentials = {"username": "integration_user", "password": SMOKE_PASSWORD}
        register = client.post("/api/v1/auth/register", json=credentials)
        ensure(register.status_code == 200, f"register failed: {register.text}")

        token = register.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        create_payload = {
            "service_name": "test_service",
            "auth_data": {"token": "abc123"},
            "endpoints": [{"name": "status", "url": "https://example.test/status"}],
            "is_active": True,
        }
        created = client.post("/api/v1/integrations", json=create_payload, headers=headers)
        ensure(created.status_code == 200, f"create integration failed: {created.text}")
        integration_id = created.json().get("id")
        ensure(bool(integration_id), "integration id is missing")

        listed = client.get("/api/v1/integrations", headers=headers)
        ensure(listed.status_code == 200, f"list integrations failed: {listed.text}")
        ensure(any(item.get("id") == integration_id for item in listed.json()), "created integration not found in list")

        call_payload = {
            "url": "https://example.test/status",
            "method": "POST",
            "payload": {"ping": "pong"},
            "headers": {"X-Test": "1"},
        }
        called = client.post(f"/api/v1/integrations/{integration_id}/call", json=call_payload, headers=headers)
        ensure(called.status_code == 200, f"integration call failed: {called.text}")
        body = called.json()
        ensure(body.get("status_code") == 200, f"unexpected call status: {called.text}")
        ensure("Bearer abc123" == body.get("headers", {}).get("Authorization"), "auth header not injected")

    await engine.dispose()
    if DB_PATH.exists():
        DB_PATH.unlink()

    print("SMOKE_INTEGRATIONS_OK")


if __name__ == "__main__":
    asyncio.run(run())
