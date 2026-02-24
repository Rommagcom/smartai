from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select

from app.api.types import DBSession
from app.core.security import create_token, get_password_hash, verify_password
from app.models.user import User
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse

router = APIRouter()


@router.post("/register")
async def register(payload: RegisterRequest, db: DBSession) -> TokenResponse:
    existing = await db.execute(select(User).where(User.username == payload.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")

    users_count_query = await db.execute(select(func.count()).select_from(User))
    users_count = int(users_count_query.scalar() or 0)

    user = User(
        username=payload.username,
        hashed_password=get_password_hash(payload.password),
        preferences={},
        is_admin=users_count == 0,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    access = create_token(str(user.id), 60, "access")
    refresh = create_token(str(user.id), 60 * 24 * 30, "refresh")
    return TokenResponse(access_token=access, refresh_token=refresh)


@router.post("/login")
async def login(payload: LoginRequest, db: DBSession) -> TokenResponse:
    result = await db.execute(select(User).where(User.username == payload.username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    access = create_token(str(user.id), 60, "access")
    refresh = create_token(str(user.id), 60 * 24 * 30, "refresh")
    return TokenResponse(access_token=access, refresh_token=refresh)
