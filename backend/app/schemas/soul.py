from datetime import datetime

from pydantic import BaseModel, Field


class SoulStatus(BaseModel):
    configured: bool
    first_question: str
    template_preview: str
    tone_options: dict[str, str]
    task_options: list[str]
    updated_at: datetime | None = None


class SoulSetupRequest(BaseModel):
    user_description: str = Field(min_length=3, max_length=4000)
    assistant_name: str | None = Field(default=None, max_length=64)
    emoji: str | None = Field(default=None, max_length=8)
    style: str = Field(default="direct")
    tone_modifier: str | None = Field(default=None, max_length=256)
    task_mode: str = Field(default="other")


class SoulAdaptTaskRequest(BaseModel):
    task_mode: str = Field(default="other")
    custom_task: str | None = Field(default=None, max_length=256)


class SoulSetupResponse(BaseModel):
    configured: bool
    assistant_name: str
    emoji: str
    style: str
    task_mode: str
    first_question: str
    system_prompt_template: str


class SoulOnboardingStep(BaseModel):
    step: str
    done: bool
    required_fields: list[str]
    next_action: str
    prompt: str
    hints: dict[str, str] = {}
