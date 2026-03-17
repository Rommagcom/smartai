import asyncio
from contextlib import suppress
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.security import get_password_hash
from app.models.cron_job import CronJob
from app.models.dynamic_tool import DynamicTool
from app.models.message import Message
from app.models.session import Session
from app.models.user import User
from app.services.chat_service import chat_service
from scripts.smoke_env import apply_smoke_env_defaults, smoke_user_uuid

apply_smoke_env_defaults()


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "smoke_graph_structured_fallback.db"


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

    try:
        async with session_factory() as db:
            user = User(
                id=smoke_user_uuid(),
                username="graph_fallback_user",
                hashed_password=get_password_hash("smoke-graph-fallback"),
                soul_configured=True,
            )
            db.add(user)
            await db.flush()

            sess = Session(user_id=user.id)
            db.add(sess)
            await db.flush()

            import app.graph as graph_module

            original_graph = graph_module.agent_graph
            original_legacy_respond = chat_service.respond

            class _FailGraph:
                async def ainvoke(self, _state):
                    raise RuntimeError("forced graph failure")

            async def _legacy_respond_should_not_be_called(*_args, **_kwargs):
                raise RuntimeError("legacy respond path must not be called in graph-only contract smoke")

            graph_module.agent_graph = _FailGraph()
            chat_service.respond = _legacy_respond_should_not_be_called
            try:
                message = (
                    "Запланируй напоминание через 5 минут что мне нужно идти домой\n"
                    "```cron_add\n"
                    "time: in 5 minutes\n"
                    "message: Пора идти домой\n"
                    "```"
                )
                answer, _memory_ids, _rag, tool_calls, artifacts = await chat_service.respond_via_graph(
                    db=db,
                    user=user,
                    session_id=sess.id,
                    user_message=message,
                )
            finally:
                graph_module.agent_graph = original_graph
                chat_service.respond = original_legacy_respond

            ensure(any(str(c.get("tool") or "") == "cron_add" and bool(c.get("success")) for c in tool_calls), f"cron_add not executed: {tool_calls}")
            ensure("напоминание" in answer.lower() or "готово" in answer.lower(), f"unexpected answer: {answer}")
            ensure(isinstance(artifacts, list), "artifacts should be a list")

            await db.commit()

        print("SMOKE_GRAPH_STRUCTURED_FALLBACK_OK")
    finally:
        try:
            with suppress(asyncio.CancelledError):
                await engine.dispose()
        except Exception as exc:
            print(f"engine dispose failed: {exc}")
        if DB_PATH.exists():
            DB_PATH.unlink()


if __name__ == "__main__":
    asyncio.run(run())
