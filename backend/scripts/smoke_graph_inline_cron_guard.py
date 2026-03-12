import asyncio
from pathlib import Path

from sqlalchemy import select
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
DB_PATH = BASE_DIR / "smoke_graph_inline_cron_guard.db"


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


class _FakeGraph:
    async def ainvoke(self, _state):
        await asyncio.sleep(0)
        # Simulate degraded graph output where LLM text contains inline cron XML
        # but graph itself produced no tool calls.
        return {
            "final_answer": "<cron_add><cron_expression>0 0 * * *</cron_expression><message>Встреча</message></cron_add>",
            "tool_calls_log": [],
            "artifacts": [],
        }


async def run() -> None:
    session_factory, engine = await init_db()

    try:
        async with session_factory() as db:
            user = User(
                id=smoke_user_uuid(),
                username="graph_inline_guard_user",
                hashed_password=get_password_hash("smoke-graph-inline-guard"),
                soul_configured=True,
            )
            db.add(user)
            await db.flush()

            sess = Session(user_id=user.id)
            db.add(sess)
            await db.flush()

            import app.graph as graph_module

            original_graph = graph_module.agent_graph
            graph_module.agent_graph = _FakeGraph()
            try:
                invalid_message = (
                    "Запланируй напоминание\n"
                    "```cron_add\n"
                    "time: sometime later\n"
                    "```"
                )
                answer, _memory_ids, _rag, tool_calls, _artifacts = await chat_service.respond_via_graph(
                    db=db,
                    user=user,
                    session_id=sess.id,
                    user_message=invalid_message,
                )
            finally:
                graph_module.agent_graph = original_graph

            ensure(
                not any(str(c.get("tool") or "") == "cron_add" and bool(c.get("success")) for c in tool_calls),
                f"invalid cron_add unexpectedly succeeded via inline bridge: {tool_calls}",
            )
            ensure("<cron_add>" in answer, f"unexpected answer normalization: {answer}")

            result = await db.execute(select(CronJob))
            rows = result.scalars().all()
            ensure(len(rows) == 0, f"cron jobs should not be created for invalid structured block, got {len(rows)}")

        print("SMOKE_GRAPH_INLINE_CRON_GUARD_OK")
    finally:
        try:
            await engine.dispose()
        except Exception as exc:
            print(f"engine dispose failed: {exc}")
        if DB_PATH.exists():
            DB_PATH.unlink()


if __name__ == "__main__":
    asyncio.run(run())
