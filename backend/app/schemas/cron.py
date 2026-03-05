from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class CronJobCreate(BaseModel):
    name: str
    cron_expression: str
    action_type: str
    payload: dict = {}
    is_active: bool = True


class CronJobOut(BaseModel):
    id: UUID
    name: str
    cron_expression: str
    action_type: str
    payload: dict
    is_active: bool
    last_run: datetime | None = None
    next_run: datetime | None = None

    model_config = {"from_attributes": True}
