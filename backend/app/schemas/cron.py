from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, field_validator


class CronJobCreate(BaseModel):
    name: str
    cron_expression: str
    action_type: str
    payload: dict = {}
    is_active: bool = True

    @field_validator("cron_expression")
    @classmethod
    def validate_cron_expression(cls, v: str) -> str:
        v = v.strip()
        if v.startswith("@once:"):
            return v
        parts = v.split()
        if len(parts) not in (5, 6):
            raise ValueError("cron_expression must have 5 or 6 fields (or start with @once:)")
        return v


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


class CronJobUpdate(BaseModel):
    name: str | None = None
    cron_expression: str | None = None
    action_type: str | None = None
    payload: dict | None = None
    is_active: bool | None = None

    @field_validator("cron_expression")
    @classmethod
    def validate_cron_expression_update(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if v.startswith("@once:"):
            return v
        parts = v.split()
        if len(parts) not in (5, 6):
            raise ValueError("cron_expression must have 5 or 6 fields (or start with @once:)")
        return v
