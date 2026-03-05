import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import get_db
from app.main import app
from app.models.cron_job import CronJob
from app.models.dynamic_tool import DynamicTool
from app.models.message import Message
from app.models.session import Session
from app.models.user import User

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "smoke_chat_cron_add_natural.db"


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def init_db() -> tuple[async_sessionmaker[AsyncSession], object]:
    if DB_PATH.exists():
        DB_PATH.unlink()

    engine = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(User.__table__.create)
        await conn.run_sync(DynamicTool.__table__.create)
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
        credentials = {"username": "chat_cron_natural_user", "password": "SmokePass123"}
        register = client.post("/api/v1/auth/register", json=credentials)
        ensure(register.status_code == 200, f"register failed: {register.text}")
        token = register.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        natural_message = "Напомни завтра в 09:00 о созвоне с командой"
        response = client.post("/api/v1/chat", json={"message": natural_message}, headers=headers)
        ensure(response.status_code == 200, f"chat natural failed: {response.text}")

        body = response.json()
        session_id = str(body.get("session_id") or "").strip()
        ensure(bool(session_id), f"session_id missing for natural phrase: {body}")
        tool_calls = body.get("tool_calls") if isinstance(body.get("tool_calls"), list) else []
        cron_calls = [
            call for call in tool_calls
            if str(call.get("tool") or "") == "cron_add"
        ]
        ensure(bool(cron_calls), f"cron_add not executed for natural phrase: {body}")
        ensure(any(bool(call.get("success")) for call in cron_calls), f"cron_add failed for natural phrase: {body}")

        listed = client.get("/api/v1/cron", headers=headers)
        ensure(listed.status_code == 200, f"cron list failed: {listed.text}")
        jobs = listed.json() if isinstance(listed.json(), list) else []
        ensure(len(jobs) >= 1, f"expected at least 1 cron job, got {len(jobs)}")

        payload_messages = [
            str((item.get("payload") or {}).get("message") or "")
            for item in jobs
            if isinstance(item, dict)
        ]
        ensure(any("созвоне с командой" in text for text in payload_messages), f"natural reminder payload not found: {payload_messages}")

        task_first_message = "Запланируй встречу на сегодня на 21:00"
        task_first_response = client.post(
            "/api/v1/chat",
            json={"message": task_first_message, "session_id": session_id},
            headers=headers,
        )
        ensure(task_first_response.status_code == 200, f"chat task-first failed: {task_first_response.text}")

        task_first_body = task_first_response.json()
        task_first_session_id = str(task_first_body.get("session_id") or "").strip()
        ensure(task_first_session_id == session_id, f"session continuity broken on task-first: {task_first_body}")
        task_first_calls = task_first_body.get("tool_calls") if isinstance(task_first_body.get("tool_calls"), list) else []
        task_first_cron_calls = [
            call for call in task_first_calls
            if str(call.get("tool") or "") == "cron_add"
        ]
        ensure(bool(task_first_cron_calls), f"cron_add not executed for task-first phrase: {task_first_body}")
        ensure(any(bool(call.get("success")) for call in task_first_cron_calls), f"cron_add failed for task-first phrase: {task_first_body}")

        listed_after_task_first = client.get("/api/v1/cron", headers=headers)
        ensure(listed_after_task_first.status_code == 200, f"cron list after task-first failed: {listed_after_task_first.text}")
        jobs_after_task_first = listed_after_task_first.json() if isinstance(listed_after_task_first.json(), list) else []
        ensure(len(jobs_after_task_first) == len(jobs) + 1, f"expected one extra cron after task-first, got {len(jobs_after_task_first)}")

        followup_message = "С женой"
        followup_response = client.post(
            "/api/v1/chat",
            json={"message": followup_message, "session_id": session_id},
            headers=headers,
        )
        ensure(followup_response.status_code == 200, f"chat follow-up failed: {followup_response.text}")
        followup_body = followup_response.json()
        followup_session_id = str(followup_body.get("session_id") or "").strip()
        ensure(followup_session_id == session_id, f"session continuity broken on follow-up: {followup_body}")

        listed_after_followup = client.get("/api/v1/cron", headers=headers)
        ensure(listed_after_followup.status_code == 200, f"cron list after follow-up failed: {listed_after_followup.text}")
        jobs_after_followup = listed_after_followup.json() if isinstance(listed_after_followup.json(), list) else []
        ensure(
            len(jobs_after_task_first) <= len(jobs_after_followup) <= (len(jobs_after_task_first) + 1),
            f"follow-up changed cron count unexpectedly: before={len(jobs_after_task_first)}, after={len(jobs_after_followup)}",
        )

        invalid_message = "Напомни когда-нибудь"
        before_invalid_count = len(jobs_after_followup)
        invalid_response = client.post(
            "/api/v1/chat",
            json={"message": invalid_message, "session_id": session_id},
            headers=headers,
        )
        ensure(invalid_response.status_code == 200, f"chat invalid failed: {invalid_response.text}")
        invalid_body = invalid_response.json()
        invalid_calls = invalid_body.get("tool_calls") if isinstance(invalid_body.get("tool_calls"), list) else []
        ensure(
            not any(str(c.get("tool") or "") == "cron_add" and bool(c.get("success")) for c in invalid_calls),
            f"invalid natural reminder unexpectedly created cron: {invalid_body}",
        )

        listed_after_invalid = client.get("/api/v1/cron", headers=headers)
        ensure(listed_after_invalid.status_code == 200, f"cron list after invalid failed: {listed_after_invalid.text}")
        jobs_after_invalid = listed_after_invalid.json() if isinstance(listed_after_invalid.json(), list) else []
        ensure(
            len(jobs_after_invalid) == before_invalid_count,
            f"invalid natural reminder changed job count unexpectedly: before={before_invalid_count}, after={len(jobs_after_invalid)}",
        )

    async with session_factory() as session:
        result = await session.execute(select(CronJob))
        rows = result.scalars().all()
        ensure(len(rows) >= 1, f"DB check failed: expected >=1 cron row, got {len(rows)}")

    try:
        await engine.dispose()
    except Exception:
        pass

    if DB_PATH.exists():
        DB_PATH.unlink()

    print("SMOKE_CHAT_CRON_ADD_NATURAL_OK")


if __name__ == "__main__":
    asyncio.run(run())
