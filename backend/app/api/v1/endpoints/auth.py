from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, not_, or_, select

from app.api.types import DBSession
from app.core.config import settings
from app.core.security import create_token, get_password_hash, verify_password
from app.models.user import User
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse

router = APIRouter()

_AUTO_ADMIN_EXCLUDED_PREFIXES: tuple[str, ...] = ("test", "demo", "sample", "example")


@router.post("/register")
async def register(payload: RegisterRequest, db: DBSession) -> TokenResponse:
    existing = await db.execute(select(User).where(User.username == payload.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")

    username_lower = payload.username.lower()
    is_excluded_from_auto_admin = any(username_lower.startswith(prefix) for prefix in _AUTO_ADMIN_EXCLUDED_PREFIXES)

    eligible_users_count_query = select(func.count()).select_from(User)
    eligible_exclusion_conditions = [User.username.ilike(f"{prefix}%") for prefix in _AUTO_ADMIN_EXCLUDED_PREFIXES]
    if eligible_exclusion_conditions:
        eligible_users_count_query = eligible_users_count_query.where(not_(or_(*eligible_exclusion_conditions)))
    eligible_users_count_result = await db.execute(eligible_users_count_query)
    eligible_users_count = int(eligible_users_count_result.scalar() or 0)

    user = User(
        username=payload.username,
        hashed_password=get_password_hash(payload.password),
        preferences={},
        is_admin=not is_excluded_from_auto_admin and eligible_users_count == 0,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    access = create_token(str(user.id), settings.ACCESS_TOKEN_EXPIRE_MINUTES, "access")
    refresh = create_token(str(user.id), settings.REFRESH_TOKEN_EXPIRE_MINUTES, "refresh")
    return TokenResponse(access_token=access, refresh_token=refresh)


@router.post("/login")
async def login(payload: LoginRequest, db: DBSession) -> TokenResponse:
    result = await db.execute(select(User).where(User.username == payload.username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    access = create_token(str(user.id), settings.ACCESS_TOKEN_EXPIRE_MINUTES, "access")
    refresh = create_token(str(user.id), settings.REFRESH_TOKEN_EXPIRE_MINUTES, "refresh")
    return TokenResponse(access_token=access, refresh_token=refresh)
