import asyncio
from pathlib import Path
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.security import get_password_hash
from app.models.cron_job import CronJob
from app.models.user import User
from app.services.chat_service import ChatService
import app.services.scheduler_service as scheduler_module

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH_PREFIX = "smoke_cron_chat_pdf_route"


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

    scheduler = scheduler_module.scheduler_service

    original_session_local = scheduler_module.AsyncSessionLocal
    original_get_or_create_session = scheduler_module.memory_service.get_or_create_session
    original_append_message = scheduler_module.memory_service.append_message
    original_send_to_user = scheduler_module.connection_manager.send_to_user
    original_worker_push = scheduler_module.worker_result_service.push

    captured: dict = {}

    class _FakeSession:
        def __init__(self) -> None:
            self.id = uuid4()

    async def fake_get_or_create_session(db, user_id, session_id):
        del db, user_id, session_id
        return _FakeSession()

    async def fake_append_message(db, user_id, session_id, role, content):
        del db, user_id, session_id, role, content

    async def fake_send_to_user(user_id, payload):
        del user_id, payload

    async def fake_worker_push(user_id, payload):
        del user_id, payload

    async def fake_respond_via_graph(db, user, session_id, user_message):
        del db, user, session_id
        captured["user_message"] = str(user_message or "")
        steps = ChatService._deterministic_tool_steps(captured["user_message"]) or []
        has_pdf_create = any(str(step.get("tool") or "") == "pdf_create" for step in steps if isinstance(step, dict))
        ensure(has_pdf_create, f"cron chat payload must route to pdf_create: {steps}")
        return (
            "Задача поставлена в очередь.",
            [],
            [],
            [{"tool": "pdf_create", "success": True, "result": {"status": "queued"}}],
            [],
        )

    scheduler_module.AsyncSessionLocal = session_factory
    scheduler_module.memory_service.get_or_create_session = fake_get_or_create_session
    scheduler_module.memory_service.append_message = fake_append_message
    scheduler_module.connection_manager.send_to_user = fake_send_to_user
    scheduler_module.worker_result_service.push = fake_worker_push

    try:
        user_id = uuid4()
        async with session_factory() as session:
            user = User(
                id=user_id,
                username="cron_pdf_route_user",
                hashed_password=get_password_hash("SmokePass123"),
                preferences={},
                is_admin=False,
            )
            session.add(user)
            await session.commit()

        from app.services.chat_service import chat_service

        original_respond_via_graph = chat_service.respond_via_graph
        chat_service.respond_via_graph = fake_respond_via_graph

        try:
            request_text = "Разработай маркетинговый план по продаже услуги AI персональный ассистент и сохрани в PDF"
            await scheduler.execute_action(
                job_id=str(uuid4()),
                user_id=str(user_id),
                action_type="chat",
                payload={"message": request_text},
            )
        finally:
            chat_service.respond_via_graph = original_respond_via_graph

        seen_message = str(captured.get("user_message") or "")
        ensure("pdf" in seen_message.lower() or "пдф" in seen_message.lower(), f"unexpected cron payload text: {seen_message}")

        print("SMOKE_CRON_CHAT_PDF_ROUTE_OK")
    finally:
        scheduler_module.AsyncSessionLocal = original_session_local
        scheduler_module.memory_service.get_or_create_session = original_get_or_create_session
        scheduler_module.memory_service.append_message = original_append_message
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
