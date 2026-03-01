from fastapi import APIRouter, Header, HTTPException, status
from sqlalchemy import select

from app.api.types import AdminUser, CurrentUser, DBSession
from app.core.config import settings
from app.models.telegram_allowed_user import TelegramAllowedUser
from app.schemas.telegram_access import TelegramAccessCheck, TelegramAllowedUserCreate, TelegramAllowedUserOut

router = APIRouter()


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
async def list_allowed_users(db: DBSession, current_user: AdminUser) -> list[TelegramAllowedUser]:
    result = await db.execute(select(TelegramAllowedUser).order_by(TelegramAllowedUser.created_at.desc()))
    return result.scalars().all()


@router.post("/admin/access", response_model=TelegramAllowedUserOut)
async def add_allowed_user(
    payload: TelegramAllowedUserCreate,
    db: DBSession,
    current_user: AdminUser,
) -> TelegramAllowedUser:

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
    current_user: AdminUser,
) -> dict:
    existing = await db.execute(select(TelegramAllowedUser).where(TelegramAllowedUser.telegram_user_id == telegram_user_id))
    item = existing.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Telegram user id not found")

    item.is_active = False
    db.add(item)
    await db.commit()
    return {"status": "disabled", "telegram_user_id": telegram_user_id}
