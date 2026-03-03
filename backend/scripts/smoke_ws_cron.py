import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import get_db
from app.main import app
from app.models.cron_job import CronJob
from app.models.user import User

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "smoke_ws_cron.db"


async def init_db() -> tuple[async_sessionmaker[AsyncSession], object]:
    if DB_PATH.exists():
        DB_PATH.unlink()

    engine = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(User.__table__.create)
        await conn.run_sync(CronJob.__table__.create)

    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False), engine


async def run() -> None:
    session_factory, engine = await init_db()

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as client:
        credentials = {"username": "wscron_user", "password": "SmokePass123"}

        register = client.post("/api/v1/auth/register", json=credentials)
        assert register.status_code == 200, register.text
        token = register.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        create_payload = {
            "name": "daily_reminder",
            "cron_expression": "*/5 * * * *",
            "action_type": "send_message",
            "payload": {"message": "smoke-cron"},
            "is_active": True,
        }
        created = client.post("/api/v1/cron", json=create_payload, headers=headers)
        assert created.status_code == 200, created.text
        job_id = created.json()["id"]

        listed = client.get("/api/v1/cron", headers=headers)
        assert listed.status_code == 200, listed.text
        assert any(job["id"] == job_id for job in listed.json()), listed.text

        deleted = client.delete(f"/api/v1/cron/{job_id}", headers=headers)
        assert deleted.status_code == 200, deleted.text

        with client.websocket_connect(f"/api/v1/ws/chat?token={token}") as ws:
            ws.send_json({"type": "ping"})
            reply = ws.receive_json()
            assert reply.get("type") == "pong", reply

    try:
        await engine.dispose()
    except BaseException:
        pass
    if DB_PATH.exists():
        DB_PATH.unlink()

    print("SMOKE_WS_CRON_OK")


if __name__ == "__main__":
    asyncio.run(run())
