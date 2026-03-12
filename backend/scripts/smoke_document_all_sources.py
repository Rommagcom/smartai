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
from app.services.tool_orchestrator_service import ToolOrchestratorService, tool_orchestrator_service
from app.workers.models import WorkerJobType
from app.workers.worker_service import worker_service
from scripts.smoke_env import apply_smoke_env_defaults, smoke_user_uuid

apply_smoke_env_defaults()

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH_PREFIX = "smoke_document_all_sources"


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
    original_summarize = ToolOrchestratorService._summarize_for_document
    captured_payloads: list[dict] = []

    async def fake_enqueue(*, job_type, payload, priority):
        ensure(job_type in {WorkerJobType.PDF_CREATE, WorkerJobType.EXCEL_CREATE}, f"unexpected job_type: {job_type}")
        captured_payloads.append({"job_type": str(job_type), "payload": dict(payload), "priority": priority})
        return {"deduplicated": False}

    async def fake_summarize(body_text: str, title_hint: str = "") -> str:
        del body_text
        return f"SUMMARY::{title_hint or 'NO_TITLE'}"

    worker_service.enqueue = fake_enqueue
    ToolOrchestratorService._summarize_for_document = staticmethod(fake_summarize)

    try:
        smoke_user_id = smoke_user_uuid()
        async with session_factory() as session:
            user = User(
                id=smoke_user_id,
                username="doc_all_sources_user",
                hashed_password=get_password_hash("SmokePass123"),
                preferences={},
                is_admin=False,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)

            steps = [
                {
                    "tool": "cron_add",
                    "arguments": {
                        "name": "chat-reminder",
                        "cron_expression": "0 9 * * *",
                        "task_text": "Проверить отчёт",
                        "action_type": "send_message",
                    },
                },
                {
                    "tool": "cron_list",
                    "arguments": {},
                },
                {
                    "tool": "pdf_create",
                    "arguments": {
                        "title": "Сводка из всех источников",
                        "content": "$all_sources",
                        "filename": "all-sources.pdf",
                    },
                },
            ]

            calls = await tool_orchestrator_service.execute_tool_chain(
                db=session,
                user=user,
                steps=steps,
                max_steps=3,
            )
            ensure(len(calls) == 3, f"unexpected calls count: {calls}")
            ensure(all(bool(item.get("success")) for item in calls), f"chain contains failed call: {calls}")

            ensure(len(captured_payloads) == 1, f"expected one queued document payload, got {captured_payloads}")
            payload = captured_payloads[0]["payload"]
            content = str(payload.get("content") or "")
            ensure(content.startswith("SUMMARY::"), f"expected summarized content, got: {content}")
            ensure("Сводка из всех источников" in content, f"title hint not propagated to summarizer: {content}")

        print("SMOKE_DOCUMENT_ALL_SOURCES_OK")
    finally:
        worker_service.enqueue = original_enqueue
        ToolOrchestratorService._summarize_for_document = original_summarize
        try:
            await engine.dispose()
        finally:
            if db_path.exists():
                db_path.unlink()


if __name__ == "__main__":
    asyncio.run(run())
