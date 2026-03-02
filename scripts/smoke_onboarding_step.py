import asyncio
import os
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import get_db
from app.main import app
from app.models.user import User

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "smoke_onboarding_step.db"
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

    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False), engine


async def run() -> None:
    session_factory, engine = await init_db()

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as client:
        credentials = {"username": "onboarding_user", "password": SMOKE_PASSWORD}
        register = client.post("/api/v1/auth/register", json=credentials)
        ensure(register.status_code == 200, f"register failed: {register.text}")

        token = register.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        me_before = client.get("/api/v1/users/me", headers=headers)
        ensure(me_before.status_code == 200, f"/users/me before setup failed: {me_before.text}")
        me_before_body = me_before.json()
        ensure(me_before_body.get("requires_soul_setup") is True, f"expected requires_soul_setup=true before setup: {me_before_body}")
        ensure(bool(me_before_body.get("soul_onboarding")), f"expected soul_onboarding payload before setup: {me_before_body}")

        step_before = client.get("/api/v1/users/me/onboarding-next-step", headers=headers)
        ensure(step_before.status_code == 200, f"step before setup failed: {step_before.text}")
        before_body = step_before.json()
        ensure(before_body.get("done") is False, f"expected not done before setup: {before_body}")
        ensure(before_body.get("step") in {"identity", "tone", "task_mode", "confirm"}, f"unexpected step before setup: {before_body}")

        setup_payload = {
            "user_description": "Я аналитик и автоматизирую отчёты",
            "assistant_name": "SOUL",
            "emoji": "🧠",
            "style": "business",
            "tone_modifier": "Деловой, структурированный",
            "task_mode": "business-analysis",
        }
        setup = client.post("/api/v1/users/me/soul/setup", json=setup_payload, headers=headers)
        ensure(setup.status_code == 200, f"soul setup failed: {setup.text}")

        step_after = client.get("/api/v1/users/me/onboarding-next-step", headers=headers)
        ensure(step_after.status_code == 200, f"step after setup failed: {step_after.text}")
        after_body = step_after.json()
        ensure(after_body.get("done") is True, f"expected done after setup: {after_body}")
        ensure(after_body.get("step") == "done", f"unexpected step after setup: {after_body}")

        me_after = client.get("/api/v1/users/me", headers=headers)
        ensure(me_after.status_code == 200, f"/users/me after setup failed: {me_after.text}")
        me_after_body = me_after.json()
        ensure(me_after_body.get("requires_soul_setup") is False, f"expected requires_soul_setup=false after setup: {me_after_body}")
        ensure(me_after_body.get("soul_onboarding") is None, f"expected no soul_onboarding payload after setup: {me_after_body}")

    try:
        await engine.dispose()
    except Exception:
        pass
    if DB_PATH.exists():
        DB_PATH.unlink()

    print("SMOKE_ONBOARDING_STEP_OK")


if __name__ == "__main__":
    asyncio.run(run())
