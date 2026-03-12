import asyncio
import io
import json
from pathlib import Path
import zipfile
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.security import get_password_hash
from app.models.dynamic_tool import DynamicTool
from app.models.user import User
from app.services.dynamic_tool_service import dynamic_tool_service

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH_PREFIX = "smoke_dynamic_skill_package"


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


def build_skill_zip() -> bytes:
    manifest = {
        "name": "weather_custom_skill",
        "version": "1.0.0",
        "entrypoint": "skill.py",
        "function": "run",
        "description": "Weather formatter skill",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
        "capabilities": {
            "llm": {"enabled": True, "max_tokens": 256, "temperature": 0.0, "allowed_models": ["default"]}
        },
    }
    skill_py = (
        "def run(params: dict, context: dict) -> dict:\n"
        "    city = params['city']\n"
        "    llm = (context or {}).get('llm')\n"
        "    if isinstance(llm, dict) and callable(llm.get('chat')):\n"
        "        out = llm['chat'](system='weather', user=f'city={city}', options={'max_tokens': 80})\n"
        "        return {'text': str(out.get('text') or ''), 'city': city}\n"
        "    return {'text': f'fallback weather for {city}', 'city': city}\n"
    )
    skill_md = "# Weather Skill\n\nFormats weather data for a city."

    buff = io.BytesIO()
    with zipfile.ZipFile(buff, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("skill.py", skill_py)
        zf.writestr("skill.md", skill_md)
    return buff.getvalue()


async def run() -> None:
    db_path = BASE_DIR / f"{DB_PATH_PREFIX}_{uuid4().hex}.db"
    session_factory, engine = await init_db(db_path)

    import app.llm as llm_module

    async def fake_chat(messages, model=None, temperature=0.0, max_tokens=128):
        del model, temperature, max_tokens
        await asyncio.sleep(0)
        user_message = ""
        if isinstance(messages, list):
            for item in messages:
                if isinstance(item, dict) and str(item.get("role") or "") == "user":
                    user_message = str(item.get("content") or "")
        return f"LLM_OK::{user_message[:80]}"

    original_chat = llm_module.llm_provider.chat
    llm_module.llm_provider.chat = fake_chat

    try:
        async with session_factory() as session:
            user = User(
                username="skill_user",
                hashed_password=get_password_hash("SmokePass123"),
                preferences={},
                is_admin=False,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)

            package = build_skill_zip()
            reg = await dynamic_tool_service.register_skill_package(
                db=session,
                user_id=user.id,
                filename="add_skill.zip",
                content=package,
            )
            ensure(str(reg.get("status") or "") in {"registered", "updated"}, f"skill registration failed: {reg}")

            call = await dynamic_tool_service.call_dynamic_tool(
                db=session,
                user_id=user.id,
                tool_name="dyn:weather_custom_skill",
                arguments={"city": "Almaty"},
            )
            ensure(bool(call.get("success")), f"skill call failed: {call}")
            data = call.get("data") if isinstance(call.get("data"), dict) else {}
            text = str(data.get("text") or "")
            ensure("LLM_OK::" in text, f"llm adapter not used: {call}")
            ensure("city=Almaty" in text, f"city payload missing in llm text: {call}")

            tool = (
                await session.execute(
                    select(DynamicTool).where(DynamicTool.user_id == user.id, DynamicTool.name == "weather_custom_skill")
                )
            ).scalar_one_or_none()
            ensure(tool is not None, "dynamic skill row was not saved")
            ensure(str(tool.method or "").upper() == "PYTHON", f"unexpected tool method: {tool.method}")

        print("SMOKE_DYNAMIC_SKILL_PACKAGE_OK")
    finally:
        llm_module.llm_provider.chat = original_chat
        try:
            await engine.dispose()
        finally:
            if db_path.exists():
                db_path.unlink()


if __name__ == "__main__":
    asyncio.run(run())
