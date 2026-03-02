import asyncio
import os
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import get_db
from app.main import app
from app.models.api_integration import ApiIntegration
from app.models.code_snippet import CodeSnippet
from app.models.cron_job import CronJob
from app.models.long_term_memory import LongTermMemory
from app.models.message import Message
from app.models.session import Session
from app.models.telegram_allowed_user import TelegramAllowedUser
from app.models.user import User
from app.models.worker_task import WorkerTask
from app.services.milvus_service import milvus_service
from app.services.scheduler_service import scheduler_service
from app.services.worker_result_service import worker_result_service

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "smoke_telegram_admin_delete.db"
SMOKE_PASSWORD = os.getenv("SMOKE_TEST_PASSWORD", "SmokePass123")


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def init_db() -> tuple[async_sessionmaker[AsyncSession], object]:
    if DB_PATH.exists():
        DB_PATH.unlink()

    engine = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(User.__table__.create)
        await conn.run_sync(TelegramAllowedUser.__table__.create)
        await conn.run_sync(Session.__table__.create)
        await conn.run_sync(Message.__table__.create)
        await conn.run_sync(LongTermMemory.__table__.create)
        await conn.run_sync(CronJob.__table__.create)
        await conn.run_sync(CodeSnippet.__table__.create)
        await conn.run_sync(ApiIntegration.__table__.create)
        await conn.run_sync(WorkerTask.__table__.create)

    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False), engine


async def run() -> None:
    session_factory, engine = await init_db()

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    original_remove_jobs_for_user = scheduler_service.remove_jobs_for_user
    original_delete_user_chunks = milvus_service.delete_user_chunks
    original_clear_user_results = worker_result_service.clear_user_results

    removed_user_ids: list[str] = []
    deleted_chunk_user_ids: list[str] = []
    cleared_result_user_ids: list[str] = []

    async def fake_clear_user_results(user_id: str) -> None:
        await asyncio.sleep(0)
        cleared_result_user_ids.append(user_id)

    scheduler_service.remove_jobs_for_user = lambda user_id: (removed_user_ids.append(user_id) or 2)
    milvus_service.delete_user_chunks = lambda user_id: (deleted_chunk_user_ids.append(user_id) or 3)
    worker_result_service.clear_user_results = fake_clear_user_results

    try:
        with TestClient(app) as client:
            admin_credentials = {"username": "admin_user", "password": SMOKE_PASSWORD}
            tg_user_id = 777001
            tg_credentials = {"username": f"tg_{tg_user_id}", "password": SMOKE_PASSWORD}

            reg_admin = client.post("/api/v1/auth/register", json=admin_credentials)
            ensure(reg_admin.status_code == 200, f"admin register failed: {reg_admin.text}")
            admin_token = reg_admin.json()["access_token"]
            admin_headers = {"Authorization": f"Bearer {admin_token}"}

            reg_tg = client.post("/api/v1/auth/register", json=tg_credentials)
            ensure(reg_tg.status_code == 200, f"telegram user register failed: {reg_tg.text}")

            allow_resp = client.post(
                "/api/v1/telegram/admin/access",
                headers=admin_headers,
                json={"telegram_user_id": tg_user_id, "note": "smoke", "is_active": True},
            )
            ensure(allow_resp.status_code == 200, f"telegram allow failed: {allow_resp.text}")

            delete_resp = client.delete(f"/api/v1/telegram/admin/users/{tg_user_id}", headers=admin_headers)
            ensure(delete_resp.status_code == 200, f"telegram full delete failed: {delete_resp.text}")
            payload = delete_resp.json()
            ensure(payload.get("status") == "deleted", f"unexpected status: {payload}")
            ensure(payload.get("telegram_user_id") == tg_user_id, f"unexpected telegram_user_id: {payload}")
            ensure(payload.get("username") == f"tg_{tg_user_id}", f"unexpected username: {payload}")
            ensure(payload.get("cleanup", {}).get("telegram_whitelist_deleted") is True, f"unexpected cleanup: {payload}")
            ensure(payload.get("cleanup", {}).get("scheduler_jobs_removed") == 2, f"unexpected cleanup: {payload}")
            ensure(payload.get("cleanup", {}).get("milvus_chunks_deleted") == 3, f"unexpected cleanup: {payload}")

            list_access_resp = client.get("/api/v1/telegram/admin/access", headers=admin_headers)
            ensure(list_access_resp.status_code == 200, f"telegram admin access list failed: {list_access_resp.text}")
            ids = [item.get("telegram_user_id") for item in list_access_resp.json()]
            ensure(tg_user_id not in ids, "telegram whitelist entry should be deleted")

            login_deleted_user = client.post("/api/v1/auth/login", json=tg_credentials)
            ensure(login_deleted_user.status_code == 401, "deleted telegram user should not login")

            second_delete_resp = client.delete(f"/api/v1/telegram/admin/users/{tg_user_id}", headers=admin_headers)
            ensure(second_delete_resp.status_code == 404, "second delete should return 404")

            ensure(len(removed_user_ids) == 1, f"scheduler cleanup call mismatch: {removed_user_ids}")
            ensure(len(deleted_chunk_user_ids) == 1, f"milvus cleanup call mismatch: {deleted_chunk_user_ids}")
            ensure(len(cleared_result_user_ids) == 1, f"worker result cleanup call mismatch: {cleared_result_user_ids}")
    finally:
        scheduler_service.remove_jobs_for_user = original_remove_jobs_for_user
        milvus_service.delete_user_chunks = original_delete_user_chunks
        worker_result_service.clear_user_results = original_clear_user_results
        app.dependency_overrides.pop(get_db, None)
        try:
            await engine.dispose()
        except Exception:
            pass
        if DB_PATH.exists():
            DB_PATH.unlink()

    print("SMOKE_TELEGRAM_ADMIN_DELETE_OK")


if __name__ == "__main__":
    asyncio.run(run())
