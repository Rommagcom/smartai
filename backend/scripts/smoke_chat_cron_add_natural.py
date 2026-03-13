import asyncio
import os
import re
from contextlib import suppress
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.security import create_token, get_password_hash
from app.db.session import get_db
from app.main import app
from app.models.cron_job import CronJob
from app.models.dynamic_tool import DynamicTool
from app.models.message import Message
from app.models.session import Session
from app.models.user import User
from scripts.smoke_env import apply_smoke_env_defaults, smoke_user_uuid

apply_smoke_env_defaults()

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "smoke_chat_cron_add_natural.db"
INLINE_CRON_PATTERN = re.compile(r"<\s*cron_add\s*>", re.IGNORECASE)


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def should_assert_persistence() -> bool:
    raw = os.getenv("SMOKE_CHAT_CRON_ADD_ASSERT_PERSISTENCE", "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def has_inline_cron_markup(body: dict) -> bool:
    response_text = str(body.get("response") or "")
    return bool(INLINE_CRON_PATTERN.search(response_text))


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
    smoke_user_id = smoke_user_uuid()

    async with session_factory() as session:
        session.add(
            User(
                id=smoke_user_id,
                username="chat_cron_natural_user",
                hashed_password=get_password_hash("SmokePass123"),
                preferences={},
                is_admin=False,
            )
        )
        await session.commit()

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as client:
        token = create_token(str(smoke_user_id), settings.ACCESS_TOKEN_EXPIRE_MINUTES, "access")
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
        executed_natural = any(bool(call.get("success")) for call in cron_calls)
        ensure(executed_natural or has_inline_cron_markup(body), f"cron_add not executed for natural phrase: {body}")

        listed = client.get("/api/v1/cron", headers=headers)
        ensure(listed.status_code == 200, f"cron list failed: {listed.text}")
        jobs = listed.json() if isinstance(listed.json(), list) else []
        assert_persistence = should_assert_persistence()
        if assert_persistence:
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
        executed_task_first = any(bool(call.get("success")) for call in task_first_cron_calls)
        ensure(executed_task_first or has_inline_cron_markup(task_first_body), f"cron_add not executed for task-first phrase: {task_first_body}")

        listed_after_task_first = client.get("/api/v1/cron", headers=headers)
        ensure(listed_after_task_first.status_code == 200, f"cron list after task-first failed: {listed_after_task_first.text}")
        jobs_after_task_first = listed_after_task_first.json() if isinstance(listed_after_task_first.json(), list) else []
        if assert_persistence:
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
        if assert_persistence:
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
        if assert_persistence:
            ensure(
                len(jobs_after_invalid) == before_invalid_count,
                f"invalid natural reminder changed job count unexpectedly: before={before_invalid_count}, after={len(jobs_after_invalid)}",
            )

        relative_once_message = "Создай напоминание через 3 минуты -Нужна встреча"
        relative_once_response = client.post(
            "/api/v1/chat",
            json={"message": relative_once_message, "session_id": session_id},
            headers=headers,
        )
        ensure(relative_once_response.status_code == 200, f"chat relative-once failed: {relative_once_response.text}")

        listed_after_relative_once = client.get("/api/v1/cron", headers=headers)
        ensure(listed_after_relative_once.status_code == 200, f"cron list after relative-once failed: {listed_after_relative_once.text}")
        jobs_after_relative_once = listed_after_relative_once.json() if isinstance(listed_after_relative_once.json(), list) else []

        once_jobs = [
            item for item in jobs_after_relative_once
            if isinstance(item, dict)
            and str((item.get("payload") or {}).get("message") or "").strip().lower() == "нужна встреча"
        ]
        ensure(bool(once_jobs), f"relative reminder payload not found: {jobs_after_relative_once}")
        ensure(
            str(once_jobs[0].get("cron_expression") or "").startswith("@once:"),
            f"relative reminder must be one-time, got: {once_jobs[0]}",
        )

        english_relative_once_message = "Schedule meeting in 2 minutes - Developer interview"
        english_relative_once_response = client.post(
            "/api/v1/chat",
            json={"message": english_relative_once_message, "session_id": session_id},
            headers=headers,
        )
        ensure(english_relative_once_response.status_code == 200, f"chat english relative-once failed: {english_relative_once_response.text}")

        listed_after_english_relative_once = client.get("/api/v1/cron", headers=headers)
        ensure(
            listed_after_english_relative_once.status_code == 200,
            f"cron list after english relative-once failed: {listed_after_english_relative_once.text}",
        )
        jobs_after_english_relative_once = (
            listed_after_english_relative_once.json() if isinstance(listed_after_english_relative_once.json(), list) else []
        )

        english_once_jobs = [
            item for item in jobs_after_english_relative_once
            if isinstance(item, dict)
            and str((item.get("payload") or {}).get("message") or "").strip().lower() == "developer interview"
        ]
        ensure(bool(english_once_jobs), f"english relative reminder payload not found: {jobs_after_english_relative_once}")
        ensure(
            str(english_once_jobs[0].get("cron_expression") or "").startswith("@once:"),
            f"english relative reminder must be one-time, got: {english_once_jobs[0]}",
        )

    if should_assert_persistence():
        async with session_factory() as session:
            result = await session.execute(select(CronJob))
            rows = result.scalars().all()
            ensure(len(rows) >= 1, f"DB check failed: expected >=1 cron row, got {len(rows)}")

    try:
        with suppress(asyncio.CancelledError):
            await engine.dispose()
    except Exception as exc:
        print(f"engine dispose failed: {exc}")

    if DB_PATH.exists():
        DB_PATH.unlink()

    print("SMOKE_CHAT_CRON_ADD_NATURAL_OK")


if __name__ == "__main__":
    asyncio.run(run())
