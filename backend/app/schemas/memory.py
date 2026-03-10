from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class MemoryCreate(BaseModel):
    fact_type: str
    content: str
    importance_score: float = Field(default=0.5, ge=0.0, le=1.0)
    expiration_date: datetime | None = None
    is_pinned: bool = False
    is_locked: bool = False


class MemoryFlagUpdate(BaseModel):
    value: bool


class MemoryCleanupResponse(BaseModel):
    deleted_count: int


class MemoryOut(BaseModel):
    id: UUID
    fact_type: str
    content: str
    importance_score: float
    expiration_date: datetime | None = None
    dedupe_key: str | None = None
    is_pinned: bool = False
    is_locked: bool = False
    pinned_at: datetime | None = None
    locked_at: datetime | None = None
    last_decay_at: datetime | None = None

    model_config = {"from_attributes": True}
