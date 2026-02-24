from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class MemoryCreate(BaseModel):
    fact_type: str
    content: str
    importance_score: float = 0.5
    expiration_date: datetime | None = None


class MemoryOut(BaseModel):
    id: UUID
    fact_type: str
    content: str
    importance_score: float
    expiration_date: datetime | None = None

    model_config = {"from_attributes": True}
