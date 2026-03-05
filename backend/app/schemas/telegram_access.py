from datetime import datetime

from pydantic import BaseModel, Field


class TelegramAllowedUserCreate(BaseModel):
    telegram_user_id: int = Field(gt=0)
    note: str | None = None
    is_active: bool = True


class TelegramAllowedUserOut(BaseModel):
    telegram_user_id: int
    note: str | None = None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class TelegramAccessCheck(BaseModel):
    telegram_user_id: int
    allowed: bool
