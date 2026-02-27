from fastapi import APIRouter, Header, HTTPException, status
from sqlalchemy import func, select

from app.api.types import CurrentUser, DBSession
from app.core.config import settings
from app.models.telegram_allowed_user import TelegramAllowedUser
from app.models.user import User
from app.schemas.telegram_access import TelegramAccessCheck, TelegramAllowedUserCreate, TelegramAllowedUserOut
from app.services.milvus_service import milvus_service
from app.services.scheduler_service import scheduler_service
from app.services.worker_result_service import worker_result_service

router = APIRouter()


def _require_admin(user: User) -> None:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")


@router.get("/access/check/{telegram_user_id}", response_model=TelegramAccessCheck)
async def telegram_access_check(
    telegram_user_id: int,
    db: DBSession,
    x_telegram_bridge_secret: str | None = Header(default=None),
) -> TelegramAccessCheck:
    if x_telegram_bridge_secret != settings.TELEGRAM_BACKEND_BRIDGE_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid bridge secret")

    result = await db.execute(
        select(TelegramAllowedUser).where(
            TelegramAllowedUser.telegram_user_id == telegram_user_id,
            TelegramAllowedUser.is_active.is_(True),
        )
    )
    allowed = result.scalar_one_or_none() is not None
    return TelegramAccessCheck(telegram_user_id=telegram_user_id, allowed=allowed)


@router.get("/admin/access", response_model=list[TelegramAllowedUserOut])
async def list_allowed_users(db: DBSession, current_user: CurrentUser) -> list[TelegramAllowedUser]:
    _require_admin(current_user)
    result = await db.execute(select(TelegramAllowedUser).order_by(TelegramAllowedUser.created_at.desc()))
    return result.scalars().all()


@router.post("/admin/access", response_model=TelegramAllowedUserOut)
async def add_allowed_user(
    payload: TelegramAllowedUserCreate,
    db: DBSession,
    current_user: CurrentUser,
) -> TelegramAllowedUser:
    _require_admin(current_user)

    existing = await db.execute(select(TelegramAllowedUser).where(TelegramAllowedUser.telegram_user_id == payload.telegram_user_id))
    item = existing.scalar_one_or_none()
    if item:
        item.is_active = payload.is_active
        item.note = payload.note
        db.add(item)
        await db.commit()
        await db.refresh(item)
        return item

    item = TelegramAllowedUser(
        telegram_user_id=payload.telegram_user_id,
        note=payload.note,
        is_active=payload.is_active,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


@router.delete("/admin/access/{telegram_user_id}")
async def disable_allowed_user(
    telegram_user_id: int,
    db: DBSession,
    current_user: CurrentUser,
) -> dict:
    _require_admin(current_user)
    existing = await db.execute(select(TelegramAllowedUser).where(TelegramAllowedUser.telegram_user_id == telegram_user_id))
    item = existing.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Telegram user id not found")

    item.is_active = False
    db.add(item)
    await db.commit()
    return {"status": "disabled", "telegram_user_id": telegram_user_id}


@router.delete("/admin/users/{telegram_user_id}")
async def admin_delete_telegram_user_fully(
    telegram_user_id: int,
    db: DBSession,
    current_user: CurrentUser,
) -> dict:
    _require_admin(current_user)

    username = f"tg_{telegram_user_id}"

    allowed_result = await db.execute(select(TelegramAllowedUser).where(TelegramAllowedUser.telegram_user_id == telegram_user_id))
    allowed_item = allowed_result.scalar_one_or_none()

    user_result = await db.execute(select(User).where(User.username == username))
    target_user = user_result.scalar_one_or_none()

    if not allowed_item and not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Telegram user id not found")

    if target_user and target_user.is_admin:
        admin_count_result = await db.execute(select(func.count()).select_from(User).where(User.is_admin.is_(True)))
        admin_count = int(admin_count_result.scalar() or 0)
        if admin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete the last admin",
            )

    if target_user:
        user_id_str = str(target_user.id)
        scheduler_jobs_removed = scheduler_service.remove_jobs_for_user(user_id=user_id_str)
        try:
            milvus_chunks_deleted = milvus_service.delete_user_chunks(user_id=user_id_str)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Telegram user deletion blocked: document storage unavailable ({exc})",
            ) from exc
        await worker_result_service.clear_user_results(user_id=user_id_str)
        await db.delete(target_user)
    else:
        user_id_str = None
        scheduler_jobs_removed = 0
        milvus_chunks_deleted = 0

    if allowed_item:
        await db.delete(allowed_item)

    await db.commit()

    return {
        "status": "deleted",
        "telegram_user_id": telegram_user_id,
        "username": username,
        "user_id": user_id_str,
        "cleanup": {
            "telegram_whitelist_deleted": bool(allowed_item),
            "scheduler_jobs_removed": scheduler_jobs_removed,
            "milvus_chunks_deleted": milvus_chunks_deleted,
            "worker_results_cleared": bool(target_user),
        },
    }
