from fastapi import APIRouter
from sqlalchemy import select

from app.api.types import CurrentUser, DBSession
from app.models.long_term_memory import LongTermMemory
from app.models.user import User
from app.schemas.memory import MemoryCreate, MemoryOut
from app.services.memory_service import memory_service

router = APIRouter()


@router.post("", response_model=MemoryOut)
async def add_memory(
    payload: MemoryCreate,
    db: DBSession,
    current_user: CurrentUser,
) -> LongTermMemory:
    memory = await memory_service.create_long_term_memory(
        db,
        current_user.id,
        payload.fact_type,
        payload.content,
        payload.importance_score,
        payload.expiration_date,
    )
    await db.commit()
    await db.refresh(memory)
    return memory


@router.get("", response_model=list[MemoryOut])
async def list_memory(
    db: DBSession,
    current_user: CurrentUser,
) -> list[LongTermMemory]:
    result = await db.execute(
        select(LongTermMemory)
        .where(LongTermMemory.user_id == current_user.id)
        .order_by(LongTermMemory.importance_score.desc(), LongTermMemory.created_at.desc())
        .limit(200)
    )
    return result.scalars().all()
