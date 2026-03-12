import asyncio
from pathlib import Path
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.security import get_password_hash
from app.graph.nodes import output_node
from app.models.user import User
from app.workers.models import WorkerJobType
from app.workers.worker_service import worker_service
from scripts.smoke_env import apply_smoke_env_defaults, smoke_user_uuid

apply_smoke_env_defaults()

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH_PREFIX = "smoke_web_export_llm_pdf"


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

    import app.db.session as db_session
    import app.graph.nodes as nodes
    from app.memory import memory_manager

    original_session_local = db_session.AsyncSessionLocal
    original_enqueue = worker_service.enqueue
    original_extract_facts = nodes.extract_facts_to_ltm
    original_append_stm = memory_manager.append_stm
    captured_payloads: list[dict] = []

    async def fake_enqueue(*, job_type, payload, priority):
        await asyncio.sleep(0)
        ensure(job_type == WorkerJobType.PDF_CREATE, f"unexpected job_type: {job_type}")
        captured_payloads.append({"payload": dict(payload), "priority": priority})
        return {"deduplicated": False}

    async def noop_extract_facts(user_id, user_message, final_answer):
        del user_id, user_message, final_answer
        await asyncio.sleep(0)

    async def noop_append_stm(user_id, user_message, assistant_message):
        del user_id, user_message, assistant_message
        await asyncio.sleep(0)

    db_session.AsyncSessionLocal = session_factory
    worker_service.enqueue = fake_enqueue
    nodes.extract_facts_to_ltm = noop_extract_facts
    memory_manager.append_stm = noop_append_stm

    try:
        smoke_user_id = smoke_user_uuid()
        async with session_factory() as session:
            user = User(
                id=smoke_user_id,
                username="web_export_llm_user",
                hashed_password=get_password_hash("SmokePass123"),
                preferences={},
                is_admin=False,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)

        llm_answer = (
            "LLM_WEB_SUMMARY:: Курс валют на сегодня\\n"
            "- EUR: 108.42\\n"
            "- USD: 99.10\\n"
            "Вывод: волатильность умеренная."
        )
        state = {
            "final_answer": llm_answer,
            "user_message": "Найди в интернете курс валют и сохрани в PDF",
            "user_id": smoke_user_id,
            "web_fetch_content": "Источник: биржевые сводки",
            "web_search_results": [{"title": "Rates", "snippet": "EUR 108.42", "url": "https://example.com"}],
            "tool_calls_log": [],
            "artifacts": [],
        }

        out = await output_node(state)

        ensure(len(captured_payloads) == 1, f"expected 1 pdf enqueue call, got: {captured_payloads}")
        payload = captured_payloads[0]["payload"]
        content = str(payload.get("content") or "")
        ensure("LLM_WEB_SUMMARY::" in content, f"LLM answer marker missing in PDF payload: {content}")
        ensure("EUR: 108.42" in content, f"LLM web facts missing in PDF payload: {content}")

        calls = out.get("tool_calls_log") if isinstance(out.get("tool_calls_log"), list) else []
        ensure(any(str(item.get("tool") or "") == "pdf_create" for item in calls), f"pdf_create call missing in output state: {calls}")
        answer = str(out.get("final_answer") or "")
        ensure("PDF" in answer and "очеред" in answer.lower(), f"expected queued export note in final answer: {answer}")

        print("SMOKE_WEB_EXPORT_LLM_PDF_OK")
    finally:
        worker_service.enqueue = original_enqueue
        db_session.AsyncSessionLocal = original_session_local
        nodes.extract_facts_to_ltm = original_extract_facts
        memory_manager.append_stm = original_append_stm
        try:
            await engine.dispose()
        finally:
            if db_path.exists():
                db_path.unlink()


if __name__ == "__main__":
    asyncio.run(run())
