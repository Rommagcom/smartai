import asyncio
from pathlib import Path
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.security import get_password_hash
from app.models.cron_job import CronJob
from app.models.dynamic_tool import DynamicTool
from app.models.message import Message
from app.models.session import Session
from app.models.user import User
from app.services.tool_orchestrator_service import tool_orchestrator_service
from app.workers.models import WorkerJobType
from app.workers.worker_service import worker_service
from scripts.smoke_env import apply_smoke_env_defaults, smoke_user_uuid

apply_smoke_env_defaults()

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH_PREFIX = "smoke_pdf_prev_placeholder_fallback"


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
    original_enqueue = worker_service.enqueue
    captured_payloads: list[dict] = []

    async def fake_enqueue(*, job_type, payload, priority, **kwargs):
        del kwargs
        ensure(job_type == WorkerJobType.PDF_CREATE, f"unexpected job_type: {job_type}")
        captured_payloads.append({"payload": dict(payload), "priority": priority})
        await asyncio.sleep(0)
        return {"deduplicated": False}

    worker_service.enqueue = fake_enqueue

    try:
        smoke_user_id = smoke_user_uuid()

        async with session_factory() as session:
            user = User(
                id=smoke_user_id,
                username="pdf_prev_fallback_user",
                hashed_password=get_password_hash("SmokePass123"),
                preferences={},
                is_admin=False,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)

            steps = [
                {
                    "tool": "pdf_create",
                    "arguments": {
                        "title": "Маркетинговый план",
                        "content": "$prev.body",
                        "filename": "marketing_plan.pdf",
                    },
                }
            ]

            calls = await tool_orchestrator_service.execute_tool_chain(
                db=session,
                user=user,
                steps=steps,
                max_steps=1,
                initial_context={
                    "_fallback_export_content": "Готовый маркетинговый план: ICP, каналы, KPI и бюджет.",
                },
            )

            ensure(len(calls) == 1, f"unexpected calls: {calls}")
            ensure(bool(calls[0].get("success")), f"pdf_create call failed: {calls}")
            ensure(len(captured_payloads) == 1, f"enqueue not captured: {captured_payloads}")
            queued_content = str(captured_payloads[0]["payload"].get("content") or "")
            ensure("маркетинговый план" in queued_content.lower(), f"fallback content not used: {queued_content}")

        print("SMOKE_PDF_PREV_PLACEHOLDER_FALLBACK_OK")
    finally:
        worker_service.enqueue = original_enqueue
        try:
            await engine.dispose()
        finally:
            if db_path.exists():
                db_path.unlink()


if __name__ == "__main__":
    asyncio.run(run())
