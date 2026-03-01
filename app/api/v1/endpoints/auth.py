from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from jose import JWTError
from sqlalchemy import func, select

from app.api.types import DBSession
from app.core.config import settings
from app.core.security import create_token, decode_token, get_password_hash, verify_password
from app.models.user import User
from app.schemas.auth import LoginRequest, RefreshRequest, RegisterRequest, TokenResponse

router = APIRouter()


def _issue_tokens(user_id: str) -> TokenResponse:
    access = create_token(user_id, settings.ACCESS_TOKEN_EXPIRE_MINUTES, "access")
    refresh = create_token(user_id, settings.REFRESH_TOKEN_EXPIRE_MINUTES, "refresh")
    return TokenResponse(access_token=access, refresh_token=refresh)


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

    return _issue_tokens(str(user.id))


@router.post("/login")
async def login(payload: LoginRequest, db: DBSession) -> TokenResponse:
    result = await db.execute(select(User).where(User.username == payload.username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    return _issue_tokens(str(user.id))


@router.post("/refresh")
async def refresh_tokens(payload: RefreshRequest, db: DBSession) -> TokenResponse:
    try:
        token_payload = decode_token(payload.refresh_token)
        if token_payload.get("type") != "refresh":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")
        user_id = UUID(token_payload.get("sub"))
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token") from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload") from exc

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    return _issue_tokens(str(user.id))
