import asyncio
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.security import get_password_hash
from app.models.dynamic_tool import DynamicTool
from app.models.user import User
from app.services.dynamic_tool_service import dynamic_tool_service
from app.services.tool_orchestrator_service import tool_orchestrator_service

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH_PREFIX = "smoke_dynamic_skill_delete"


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

    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False), engine


async def _create_tool(db: AsyncSession, user_id, name: str) -> DynamicTool:
    tool = await dynamic_tool_service.create_tool(
        db=db,
        user_id=user_id,
        name=name,
        description=f"Tool {name}",
        endpoint=f"https://example.test/{name}",
        method="GET",
        parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    await db.commit()
    await db.refresh(tool)
    return tool


async def _count_tools(db: AsyncSession, user_id) -> int:
    rows = await dynamic_tool_service.list_tools(db=db, user_id=user_id, active_only=False)
    return len(rows)


async def run() -> None:
    db_path = BASE_DIR / f"{DB_PATH_PREFIX}_{uuid4().hex}.db"
    session_factory, engine = await init_db(db_path)

    try:
        async with session_factory() as session:
            admin = User(
                username="skill_admin",
                hashed_password=get_password_hash("SmokePass123"),
                preferences={},
                is_admin=True,
            )
            session.add(admin)
            await session.commit()
            await session.refresh(admin)

            tool_name = "weather_custom_skill"
            tool_by_name = await _create_tool(session, admin.id, tool_name)

            # 1) Delete one by tool_name
            result_by_name = await tool_orchestrator_service._dynamic_tool_delete(
                db=session,
                user=admin,
                arguments={"tool_name": tool_name},
            )
            ensure(bool(result_by_name.get("deleted")), f"delete by tool_name failed: {result_by_name}")
            left_after_name = (
                await session.execute(
                    select(DynamicTool).where(DynamicTool.user_id == admin.id, DynamicTool.id == tool_by_name.id)
                )
            ).scalar_one_or_none()
            ensure(left_after_name is None, "tool still exists after delete by tool_name")

            # 2) Delete one by skill_name alias
            alias_name = "reminder_skill"
            tool_by_alias = await _create_tool(session, admin.id, alias_name)
            result_by_alias = await tool_orchestrator_service._dynamic_tool_delete(
                db=session,
                user=admin,
                arguments={"skill_name": alias_name},
            )
            ensure(bool(result_by_alias.get("deleted")), f"delete by skill_name failed: {result_by_alias}")
            left_after_alias = (
                await session.execute(
                    select(DynamicTool).where(DynamicTool.user_id == admin.id, DynamicTool.id == tool_by_alias.id)
                )
            ).scalar_one_or_none()
            ensure(left_after_alias is None, "tool still exists after delete by skill_name")

            # 3) Delete one by tool_id
            by_id_name = "fx_rates_skill"
            tool_by_id = await _create_tool(session, admin.id, by_id_name)
            result_by_id = await tool_orchestrator_service._dynamic_tool_delete(
                db=session,
                user=admin,
                arguments={"tool_id": str(tool_by_id.id)},
            )
            ensure(bool(result_by_id.get("deleted")), f"delete by tool_id failed: {result_by_id}")

            # 4) Delete all
            await _create_tool(session, admin.id, "cleanup_skill_a")
            await _create_tool(session, admin.id, "cleanup_skill_b")
            before_all = await _count_tools(session, admin.id)
            ensure(before_all >= 2, f"expected at least 2 skills before delete_all, got {before_all}")

            result_all = await tool_orchestrator_service._dynamic_tool_delete_all(
                db=session,
                user=admin,
                arguments={},
            )
            deleted_count = int(result_all.get("deleted_count") or 0)
            ensure(deleted_count >= 2, f"delete_all deleted too few skills: {result_all}")
            after_all = await _count_tools(session, admin.id)
            ensure(after_all == 0, f"skills still remain after delete_all: {after_all}")

            # 5) Non-admin protection
            user = User(
                username="skill_user",
                hashed_password=get_password_hash("SmokePass123"),
                preferences={},
                is_admin=False,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)

            await _create_tool(session, user.id, "user_tool")
            forbidden = await tool_orchestrator_service._dynamic_tool_delete_all(
                db=session,
                user=user,
                arguments={},
            )
            ensure(
                str(forbidden.get("status") or "") == "forbidden",
                f"non-admin delete_all must be forbidden: {forbidden}",
            )

        print("SMOKE_DYNAMIC_SKILL_DELETE_OK")
    finally:
        try:
            await engine.dispose()
        finally:
            if db_path.exists():
                db_path.unlink()


if __name__ == "__main__":
    asyncio.run(run())
