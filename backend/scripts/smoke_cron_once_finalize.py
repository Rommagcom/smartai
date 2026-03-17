import asyncio
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.security import get_password_hash
from app.models.cron_job import CronJob
from app.models.user import User
import app.services.scheduler_service as scheduler_module

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH_PREFIX = "smoke_cron_once_finalize"


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def init_db(db_path: Path) -> tuple[async_sessionmaker[AsyncSession], object]:
    if db_path.exists():
        db_path.unlink()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(User.__table__.create)
        await conn.run_sync(CronJob.__table__.create)

    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False), engine


async def run() -> None:
    db_path = BASE_DIR / f"{DB_PATH_PREFIX}_{uuid4().hex}.db"
    session_factory, engine = await init_db(db_path)

    original_session_local = scheduler_module.AsyncSessionLocal
    scheduler = scheduler_module.scheduler_service
    original_acquire_execution_lock = scheduler._acquire_execution_lock
    original_send_to_user = scheduler_module.connection_manager.send_to_user
    original_worker_push = scheduler_module.worker_result_service.push

    # Bind scheduler DB access to the isolated sqlite test database.
    scheduler_module.AsyncSessionLocal = session_factory

    async def _always_lock(*, job_id: str, action_type: str) -> bool:
        del job_id, action_type
        return True

    async def _noop_send_to_user(user_id: str, payload: dict) -> None:
        del user_id, payload

    async def _noop_worker_push(user_id: str, payload: dict) -> None:
        del user_id, payload

    scheduler._acquire_execution_lock = _always_lock
    scheduler_module.connection_manager.send_to_user = _noop_send_to_user
    scheduler_module.worker_result_service.push = _noop_worker_push

    try:
        user_id = uuid4()
        cron_id = uuid4()
        cron_expression = "@once:2030-01-01T00:00:00+00:00"

        async with session_factory() as session:
            user = User(
                id=user_id,
                username="cron_once_finalize_user",
                hashed_password=get_password_hash("SmokePass123"),
                preferences={},
                is_admin=False,
            )
            session.add(user)
            session.add(
                CronJob(
                    id=cron_id,
                    user_id=user_id,
                    name="chat-reminder",
                    cron_expression=cron_expression,
                    action_type="send_message",
                    payload={"message": "Выезжать за сыном"},
                    is_active=True,
                )
            )
            await session.commit()

        # Add to in-memory scheduler and execute once.
        scheduler.add_or_replace_job(
            job_id=str(cron_id),
            cron_expression=cron_expression,
            user_id=str(user_id),
            action_type="send_message",
            payload={"message": "Выезжать за сыном"},
        )
        ensure(scheduler.scheduler.get_job(str(cron_id)) is not None, "cron job was not scheduled")

        await scheduler.execute_action(
            job_id=str(cron_id),
            user_id=str(user_id),
            action_type="send_message",
            payload={"message": "Выезжать за сыном"},
        )

        async with session_factory() as session:
            result = await session.execute(select(CronJob).where(CronJob.id == cron_id))
            row = result.scalar_one_or_none()
            ensure(row is not None, "cron row not found after execute_action")
            ensure(not bool(row.is_active), f"once cron must become inactive after run, got is_active={row.is_active}")
            ensure(row.last_run is not None, "once cron must record last_run timestamp")

        sync_result = await scheduler._sync_jobs_from_db_internal(force_reload_all=True)
        ensure(int(sync_result.get("loaded", 0)) == 0, f"inactive once cron must not be reloaded: {sync_result}")
        ensure(scheduler.scheduler.get_job(str(cron_id)) is None, "inactive once cron must not remain in scheduler")

        print("SMOKE_CRON_ONCE_FINALIZE_OK")
    finally:
        scheduler_module.AsyncSessionLocal = original_session_local
        scheduler._acquire_execution_lock = original_acquire_execution_lock
        scheduler_module.connection_manager.send_to_user = original_send_to_user
        scheduler_module.worker_result_service.push = original_worker_push
        try:
            scheduler.scheduler.remove_all_jobs()
        except Exception:
            pass
        await engine.dispose()
        if db_path.exists():
            db_path.unlink()


if __name__ == "__main__":
    asyncio.run(run())
