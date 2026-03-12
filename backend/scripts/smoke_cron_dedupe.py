import asyncio
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.security import get_password_hash
from app.models.cron_job import CronJob
from app.models.dynamic_tool import DynamicTool
from app.models.message import Message
from app.models.session import Session
from app.models.user import User
from app.services.tool_orchestrator_service import tool_orchestrator_service
from scripts.smoke_env import apply_smoke_env_defaults, smoke_user_uuid

apply_smoke_env_defaults()

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH_PREFIX = "smoke_cron_dedupe"


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def init_db(db_path: Path) -> tuple[async_sessionmaker[AsyncSession], object]:
    if db_path.exists():
        db_path.unlink()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(User.__table__.create)
        await conn.run_sync(DynamicTool.__table__.create)
        await conn.run_sync(Session.__table__.create)
        await conn.run_sync(Message.__table__.create)
        await conn.run_sync(CronJob.__table__.create)

    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False), engine


async def run() -> None:
    db_path = BASE_DIR / f"{DB_PATH_PREFIX}_{uuid4().hex}.db"
    session_factory, engine = await init_db(db_path)

    try:
        smoke_user_id = smoke_user_uuid()

        async with session_factory() as session:
            user = User(
                id=smoke_user_id,
                username="cron_dedupe_user",
                hashed_password=get_password_hash("SmokePass123"),
                preferences={},
                is_admin=False,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)

            step = {
                "tool": "cron_add",
                "arguments": {
                    "name": "chat-reminder",
                    "cron_expression": "0 9 * * *",
                    "task_text": "Курс валют",
                    "action_type": "send_message",
                },
            }

            first = await tool_orchestrator_service.execute_tool_chain(
                db=session,
                user=user,
                steps=[step],
                max_steps=1,
            )
            ensure(len(first) == 1 and bool(first[0].get("success")), f"first cron_add failed: {first}")
            first_result = first[0].get("result") if isinstance(first[0].get("result"), dict) else {}
            ensure(first_result.get("deduplicated") is False, f"first cron_add unexpectedly deduplicated: {first}")

            second = await tool_orchestrator_service.execute_tool_chain(
                db=session,
                user=user,
                steps=[step],
                max_steps=1,
            )
            ensure(len(second) == 1 and bool(second[0].get("success")), f"second cron_add failed: {second}")
            second_result = second[0].get("result") if isinstance(second[0].get("result"), dict) else {}
            ensure(second_result.get("deduplicated") is True, f"second cron_add not deduplicated: {second}")
            ensure(
                str(first_result.get("id") or "") == str(second_result.get("id") or ""),
                f"dedupe should return same cron id: first={first_result}, second={second_result}",
            )

            rows = (await session.execute(select(CronJob))).scalars().all()
            ensure(len(rows) == 1, f"expected exactly one cron row after dedupe, got {len(rows)}")

            await session.commit()

        print("SMOKE_CRON_DEDUPE_OK")
    finally:
        try:
            await engine.dispose()
        finally:
            if db_path.exists():
                db_path.unlink()


if __name__ == "__main__":
    asyncio.run(run())
