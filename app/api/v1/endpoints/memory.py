from uuid import UUID

from fastapi import APIRouter, HTTPException

from app.api.types import CurrentUser, DBSession
from app.models.long_term_memory import LongTermMemory
from app.schemas.memory import MemoryCleanupResponse, MemoryCreate, MemoryFlagUpdate, MemoryOut
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
        payload.is_pinned,
        payload.is_locked,
    )
    await db.commit()
    await db.refresh(memory)
    return memory


@router.get("", response_model=list[MemoryOut])
async def list_memory(
    db: DBSession,
    current_user: CurrentUser,
) -> list[LongTermMemory]:
    return await memory_service.list_memories(db=db, user_id=current_user.id, limit=200)


@router.patch("/{memory_id}/pin", response_model=MemoryOut)
async def pin_memory(
    memory_id: UUID,
    payload: MemoryFlagUpdate,
    db: DBSession,
    current_user: CurrentUser,
) -> LongTermMemory:
    memory = await memory_service.set_memory_pin(db=db, user_id=current_user.id, memory_id=memory_id, value=payload.value)
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")
    await db.commit()
    await db.refresh(memory)
    return memory


@router.patch("/{memory_id}/lock", response_model=MemoryOut)
async def lock_memory(
    memory_id: UUID,
    payload: MemoryFlagUpdate,
    db: DBSession,
    current_user: CurrentUser,
) -> LongTermMemory:
    memory = await memory_service.set_memory_lock(db=db, user_id=current_user.id, memory_id=memory_id, value=payload.value)
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")
    await db.commit()
    await db.refresh(memory)
    return memory


@router.post("/cleanup", response_model=MemoryCleanupResponse)
async def cleanup_memory(
    db: DBSession,
    current_user: CurrentUser,
    limit: int = 500,
) -> MemoryCleanupResponse:
    deleted_count = await memory_service.cleanup_expired_memories(db=db, user_id=current_user.id, limit=limit)
    await db.commit()
    return MemoryCleanupResponse(deleted_count=deleted_count)


@router.delete("/{memory_id}", responses={404: {"description": "Memory not found"}})
async def delete_memory(
    memory_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
) -> dict:
    from sqlalchemy import select
    from app.models.long_term_memory import LongTermMemory as LTM

    result = await db.execute(
        select(LTM).where(LTM.id == memory_id, LTM.user_id == current_user.id)
    )
    memory = result.scalar_one_or_none()
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")

    await db.delete(memory)
    await db.commit()
    return {"status": "deleted", "memory_id": str(memory_id)}
