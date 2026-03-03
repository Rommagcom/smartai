from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class UserOut(BaseModel):
    id: UUID
    username: str
    is_admin: bool
    preferences: dict
    soul_profile: dict
    soul_configured: bool
    requires_soul_setup: bool
    soul_onboarding: dict | None = None
    system_prompt_template: str
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class UserPreferencesUpdate(BaseModel):
    preferences: dict


class UserAdminAccessUpdate(BaseModel):
    is_admin: bool
