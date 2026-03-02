import asyncio
import os
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
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

    original_api_call = api_executor.call
    app.dependency_overrides[get_db] = override_get_db
    api_executor.call = fake_call

    async def ensure_admin(username: str) -> None:
        async with session_factory() as db:
            user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
            ensure(user is not None, "user not found for admin elevation")
            user.is_admin = True
            db.add(user)
            await db.commit()

    try:
      with TestClient(app) as client:
        credentials = {"username": "integration_user", "password": SMOKE_PASSWORD}
        register = client.post("/api/v1/auth/register", json=credentials)
        ensure(register.status_code == 200, f"register failed: {register.text}")

        await ensure_admin("integration_user")

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

        onboarding_connect_payload = {
            "service_name": "onboarding_service",
            "token": "onboard-token",
            "base_url": "https://example.test",
            "endpoints": [{"name": "ping", "url": "https://example.test/ping", "method": "GET"}],
            "healthcheck": {"url": "https://example.test/health", "method": "GET"},
        }
        connected = client.post("/api/v1/integrations/onboarding/connect", json=onboarding_connect_payload, headers=headers)
        ensure(connected.status_code == 200, f"onboarding connect failed: {connected.text}")
        connected_payload = connected.json()
        draft = connected_payload.get("draft") or {}
        draft_id = str(connected_payload.get("draft_id") or "")
        ensure(bool(draft_id), f"draft_id is missing: {connected.text}")
        ensure(draft.get("service_name") == "onboarding_service", f"invalid onboarding draft: {connected.text}")

        status_connected = client.get(f"/api/v1/integrations/onboarding/status/{draft_id}", headers=headers)
        ensure(status_connected.status_code == 200, f"onboarding status (connected) failed: {status_connected.text}")
        ensure(status_connected.json().get("step") == "connected", f"expected connected step: {status_connected.text}")

        tested = client.post("/api/v1/integrations/onboarding/test", json={"draft_id": draft_id}, headers=headers)
        ensure(tested.status_code == 200, f"onboarding test failed: {tested.text}")
        test_payload = tested.json().get("test") or {}
        ensure(test_payload.get("success") is True, f"onboarding healthcheck should pass: {tested.text}")

        status_tested = client.get(f"/api/v1/integrations/onboarding/status/{draft_id}", headers=headers)
        ensure(status_tested.status_code == 200, f"onboarding status (tested) failed: {status_tested.text}")
        ensure(status_tested.json().get("step") == "tested", f"expected tested step: {status_tested.text}")

        saved = client.post(
            "/api/v1/integrations/onboarding/save",
            json={"draft_id": draft_id, "is_active": True, "require_successful_test": True},
            headers=headers,
        )
        ensure(saved.status_code == 200, f"onboarding save failed: {saved.text}")
        saved_integration = saved.json().get("integration") or {}
        saved_integration_id = saved_integration.get("id")
        ensure(bool(saved_integration_id), f"saved integration id missing: {saved.text}")

        status_saved = client.get(f"/api/v1/integrations/onboarding/status/{draft_id}", headers=headers)
        ensure(status_saved.status_code == 200, f"onboarding status (saved) failed: {status_saved.text}")
        status_saved_payload = status_saved.json()
        ensure(status_saved_payload.get("step") == "saved", f"expected saved step: {status_saved.text}")
        ensure(status_saved_payload.get("saved_integration_id") == saved_integration_id, f"saved integration mismatch: {status_saved.text}")

        health = client.get(f"/api/v1/integrations/{saved_integration_id}/health", headers=headers)
        ensure(health.status_code == 200, f"integration health failed: {health.text}")
        health_payload = health.json().get("health") or {}
        ensure(health_payload.get("success") is True, f"saved integration health should pass: {health.text}")

        rotate = client.post("/api/v1/integrations/admin/rotate-auth-data", headers=headers)
        ensure(rotate.status_code == 200, f"admin rotate auth_data failed: {rotate.text}")
        rotate_payload = rotate.json()
        ensure(int(rotate_payload.get("scanned") or 0) >= 1, f"rotation scanned should be >=1: {rotate.text}")

        print("SMOKE_INTEGRATIONS_OK")
    finally:
        app.dependency_overrides.pop(get_db, None)
        api_executor.call = original_api_call
        try:
            await engine.dispose()
        except Exception:
            pass
        if DB_PATH.exists():
            DB_PATH.unlink()


if __name__ == "__main__":
    asyncio.run(run())
