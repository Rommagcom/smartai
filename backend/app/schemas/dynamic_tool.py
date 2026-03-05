"""Pydantic v2 schemas for Dynamic Tool Injection."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


# ---------- LLM-generated registration schema ----------

class ApiRegistrationPayload(BaseModel):
    """Schema that the LLM fills when the user describes a new API."""

    tool_name: str = Field(..., description="Имя инструмента (латиница, snake_case, без пробелов)")
    description: str = Field(..., description="Описание инструмента на русском")
    api_endpoint: str = Field(..., description="URL эндпоинта API")
    method: str = Field("GET", description="HTTP-метод: GET, POST, PUT, PATCH, DELETE")
    headers: dict = Field(default_factory=dict, description="HTTP-заголовки")
    auth_token: str | None = Field(None, description="Bearer-токен, если указан")
    parameters_schema: dict = Field(
        default_factory=dict,
        description="JSON Schema параметров API ({type: object, properties: {...}})",
    )
    response_hint: str = Field(
        "",
        description="Краткое описание формата ответа API, если пользователь его указал",
    )


# ---------- REST API schemas ----------

class DynamicToolCreate(BaseModel):
    name: str
    description: str = ""
    endpoint: str
    method: str = "GET"
    headers: dict = Field(default_factory=dict)
    auth_token: str | None = None
    parameters_schema: dict = Field(default_factory=dict)
    response_hint: str = ""


class DynamicToolUpdate(BaseModel):
    description: str | None = None
    endpoint: str | None = None
    method: str | None = None
    headers: dict | None = None
    auth_token: str | None = None
    parameters_schema: dict | None = None
    response_hint: str | None = None
    is_active: bool | None = None


class DynamicToolOut(BaseModel):
    id: UUID
    name: str
    description: str
    endpoint: str
    method: str
    parameters_schema: dict
    response_hint: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class DynamicToolBrief(BaseModel):
    """Compact format shown to the LLM planner."""

    name: str
    description: str
    parameters_schema: dict
