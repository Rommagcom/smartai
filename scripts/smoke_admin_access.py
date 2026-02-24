import asyncio
import os
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import get_db
from app.main import app
from app.models.user import User

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "smoke_admin_access.db"
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

    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False), engine


async def run() -> None:
    session_factory, engine = await init_db()

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as client:
        admin_credentials = {"username": "admin_user", "password": SMOKE_PASSWORD}
        user_credentials = {"username": "regular_user", "password": SMOKE_PASSWORD}

        reg_admin = client.post("/api/v1/auth/register", json=admin_credentials)
        ensure(reg_admin.status_code == 200, f"admin register failed: {reg_admin.text}")
        admin_token = reg_admin.json()["access_token"]

        reg_user = client.post("/api/v1/auth/register", json=user_credentials)
        ensure(reg_user.status_code == 200, f"regular register failed: {reg_user.text}")
        user_token = reg_user.json()["access_token"]

        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        user_headers = {"Authorization": f"Bearer {user_token}"}

        me_admin = client.get("/api/v1/users/me", headers=admin_headers)
        ensure(me_admin.status_code == 200, f"me admin failed: {me_admin.text}")
        admin_id = me_admin.json()["id"]
        ensure(me_admin.json().get("is_admin") is True, "first user should be admin")

        me_user = client.get("/api/v1/users/me", headers=user_headers)
        ensure(me_user.status_code == 200, f"me user failed: {me_user.text}")
        user_id = me_user.json()["id"]
        ensure(me_user.json().get("is_admin") is False, "second user should not be admin by default")

        list_by_non_admin = client.get("/api/v1/users/admin/users", headers=user_headers)
        ensure(list_by_non_admin.status_code == 403, f"non-admin should be denied: {list_by_non_admin.text}")

        list_by_admin = client.get("/api/v1/users/admin/users", headers=admin_headers)
        ensure(list_by_admin.status_code == 200, f"admin list failed: {list_by_admin.text}")
        users = list_by_admin.json()
        ensure(any(item.get("id") == admin_id for item in users), "admin user should be in list")
        ensure(any(item.get("id") == user_id for item in users), "regular user should be in list")

        revoke_last_admin = client.patch(
            f"/api/v1/users/admin/users/{admin_id}/admin-access",
            headers=admin_headers,
            json={"is_admin": False},
        )
        ensure(revoke_last_admin.status_code == 400, f"should not revoke last admin: {revoke_last_admin.text}")

        grant_user_admin = client.patch(
            f"/api/v1/users/admin/users/{user_id}/admin-access",
            headers=admin_headers,
            json={"is_admin": True},
        )
        ensure(grant_user_admin.status_code == 200, f"grant admin failed: {grant_user_admin.text}")
        ensure(grant_user_admin.json().get("is_admin") is True, "regular user should become admin")

        revoke_initial_admin = client.patch(
            f"/api/v1/users/admin/users/{admin_id}/admin-access",
            headers=user_headers,
            json={"is_admin": False},
        )
        ensure(revoke_initial_admin.status_code == 200, f"new admin should revoke old admin: {revoke_initial_admin.text}")

        revoke_last_remaining = client.patch(
            f"/api/v1/users/admin/users/{user_id}/admin-access",
            headers=user_headers,
            json={"is_admin": False},
        )
        ensure(revoke_last_remaining.status_code == 400, f"should protect last remaining admin: {revoke_last_remaining.text}")

    await engine.dispose()
    if DB_PATH.exists():
        DB_PATH.unlink()

    print("SMOKE_ADMIN_ACCESS_OK")


if __name__ == "__main__":
    asyncio.run(run())
