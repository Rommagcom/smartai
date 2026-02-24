from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class IntegrationCreate(BaseModel):
    service_name: str
    auth_data: dict
    endpoints: list[dict] = []
    is_active: bool = True


class IntegrationOut(BaseModel):
    id: UUID
    service_name: str
    endpoints: list
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}
