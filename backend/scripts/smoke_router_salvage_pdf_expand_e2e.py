import asyncio
from pathlib import Path
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.security import get_password_hash
from app.graph.nodes import router_node
from app.llm import llm_provider
from app.models.user import User
import app.services.tool_orchestrator_service as tool_module
from app.services.tool_orchestrator_service import tool_orchestrator_service
from app.workers.models import WorkerJobType
from app.workers.worker_service import worker_service
from scripts.smoke_env import apply_smoke_env_defaults

apply_smoke_env_defaults()

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH_PREFIX = "smoke_router_salvage_pdf_expand_e2e"


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def init_db(db_path: Path) -> tuple[async_sessionmaker[AsyncSession], object]:
    if db_path.exists():
        db_path.unlink()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(User.__table__.create)

    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False), engine


async def run() -> None:
    db_path = BASE_DIR / f"{DB_PATH_PREFIX}_{uuid4().hex}.db"
    session_factory, engine = await init_db(db_path)

    original_chat_structured = llm_provider.chat_structured
    original_expand = tool_module._expand_prompt_to_content
    original_enqueue = worker_service.enqueue

    original_shortcuts = settings.ROUTER_ENABLE_DETERMINISTIC_SHORTCUTS
    original_fallbacks = settings.ROUTER_ENABLE_DETERMINISTIC_FALLBACKS
    original_salvage = settings.ROUTER_ENABLE_LLM_PARSE_SALVAGE

    captured_payloads: list[dict] = []

    async def broken_router_structured(*args, **kwargs):
        del args, kwargs
        await asyncio.sleep(0)
        raise ValueError(
            "Failed to parse LLM response into RouterOutput after 2 attempts: "
            "Structured JSON payload not found in LLM response: "
            "{\"decision\":\"tool\",\"steps\":[{\"tool\":\"pdf_create\","
            "\"arguments\":{\"title\":\"Маркетинговый план\","
            "\"content\":\"Разработай маркетинговый план и сохрани в pdf\"}}]}"
        )

    async def fake_expand(prompt_text: str, title_hint: str = "") -> str:
        del prompt_text
        title = title_hint or "Документ"
        return (
            f"## {title}\n"
            "### Цели\n"
            "1. Лидогенерация\n"
            "2. Конверсия в демо\n"
            "### Каналы\n"
            "- Контент-маркетинг\n"
            "- Партнерские продажи\n"
            "- Telegram-воронка\n"
        )

    async def fake_enqueue(*, job_type, payload, priority):
        ensure(job_type == WorkerJobType.PDF_CREATE, f"unexpected job_type: {job_type}")
        captured_payloads.append({"payload": dict(payload), "priority": str(priority)})
        return {"deduplicated": False}

    llm_provider.chat_structured = broken_router_structured
    tool_module._expand_prompt_to_content = fake_expand
    worker_service.enqueue = fake_enqueue

    settings.ROUTER_ENABLE_DETERMINISTIC_SHORTCUTS = False
    settings.ROUTER_ENABLE_DETERMINISTIC_FALLBACKS = False
    settings.ROUTER_ENABLE_LLM_PARSE_SALVAGE = True

    try:
        user_message = (
            "Разработай маркетинговый план по продаже услуги AI персональный ассистент "
            "как ты с твоим функционалом и сохрани в pdf"
        )

        state = {
            "user_message": user_message,
            "user_id": None,
            "feedback_plan": "",
            "retrieved_tools": [],
            "history_messages": [],
        }
        out = await router_node(state)
        router_output = out.get("router_output")

        ensure(out.get("next_step") == "tool", f"router fallback must route to tool: {out}")
        ensure(router_output is not None, f"router output missing: {out}")
        steps = router_output.steps if hasattr(router_output, "steps") else []
        ensure(len(steps) == 1 and str(steps[0].tool) == "pdf_create", f"unexpected router steps: {steps}")

        async with session_factory() as session:
            user = User(
                id=uuid4(),
                username="router_salvage_pdf_expand_user",
                hashed_password=get_password_hash("SmokePass123"),
                preferences={},
                is_admin=False,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)

            calls = await tool_orchestrator_service.execute_tool_chain(
                db=session,
                user=user,
                steps=[{"tool": step.tool, "arguments": step.arguments} for step in steps],
                max_steps=1,
            )

            ensure(len(calls) == 1, f"unexpected calls count: {calls}")
            ensure(bool(calls[0].get("success")), f"pdf step must succeed: {calls}")
            ensure(str((calls[0].get("result") or {}).get("status") or "") == "queued", f"unexpected result: {calls}")

        ensure(len(captured_payloads) == 1, f"worker enqueue not captured: {captured_payloads}")
        payload = captured_payloads[0]["payload"]
        content = str(payload.get("content") or "").strip()

        ensure(content, f"empty worker payload content: {payload}")
        ensure("## " in content, f"content was not expanded to structured text: {content}")
        ensure("сохрани в pdf" not in content.lower(), f"raw command leaked into payload: {content}")

        print("SMOKE_ROUTER_SALVAGE_PDF_EXPAND_E2E_OK")
    finally:
        llm_provider.chat_structured = original_chat_structured
        tool_module._expand_prompt_to_content = original_expand
        worker_service.enqueue = original_enqueue

        settings.ROUTER_ENABLE_DETERMINISTIC_SHORTCUTS = original_shortcuts
        settings.ROUTER_ENABLE_DETERMINISTIC_FALLBACKS = original_fallbacks
        settings.ROUTER_ENABLE_LLM_PARSE_SALVAGE = original_salvage

        try:
            await engine.dispose()
        finally:
            if db_path.exists():
                db_path.unlink()


if __name__ == "__main__":
    asyncio.run(run())
