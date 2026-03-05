"""register_api_tool service — LLM-driven API tool registration with vector storage.

Orchestrates the full registration flow:
1. LLM extracts ``RegisterApiToolInput`` from natural-language description
2. SSRF validation on the endpoint URL
3. Persists tool in PostgreSQL (``DynamicTool``)
4. Generates embedding and saves to Milvus (``tool_vectors`` collection)
5. Returns confirmation for the compose node

This replaces/enhances the old ``dynamic_tool_register`` flow by adding
Milvus vector storage so the retriever node can semantically discover
relevant tools at query time.
"""

from __future__ import annotations

import logging
import re
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.schemas.tool_registry import RegisterApiToolInput
from app.services.egress_policy_service import egress_policy_service
from app.services.vector_tool_registry import vector_tool_registry

logger = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")

# System prompt for LLM-based API spec extraction
_REGISTRATION_SYSTEM_PROMPT = """\
Ты — продвинутый ИИ-оркестратор. Задача: извлечь структурированное описание API из сообщения пользователя.

### ПРАВИЛА:
- Верни строго JSON без markdown.
- Формат: {"tool_name": "...", "description": "...", "api_endpoint": "...", "method": "GET|POST", "headers": {}, "auth_token": null, "parameters_schema": {"type": "object", "properties": {...}, "required": [...]}, "response_hint": "..."}
- tool_name: латиница, snake_case, без пробелов, 2-63 символа.
- Для каждого параметра добавляй description и правильный type (string, number, boolean, integer).
- Если пользователь указал токен — помести в auth_token.
- Если данных недостаточно — заполни tool_name и description на основе имеющейся информации.

### ПРИМЕР:
Пользователь: "Подключи API погоды https://api.weather.com/v1, параметр city (строка)".
Ответ: {"tool_name": "weather_api", "description": "Получение прогноза погоды по городу", "api_endpoint": "https://api.weather.com/v1", "method": "GET", "headers": {}, "auth_token": null, "parameters_schema": {"type": "object", "properties": {"city": {"type": "string", "description": "Город"}}, "required": ["city"]}, "response_hint": "JSON с данными о погоде"}
"""


class RegisterApiToolService:
    """Handles the full register_api_tool flow: LLM extraction → DB → Milvus."""

    async def register_from_message(
        self,
        db: AsyncSession,
        user_id: UUID,
        user_message: str,
    ) -> dict:
        """Parse user message via LLM and register the tool in DB + Milvus.

        Returns a result dict suitable for tool_result composition.
        """
        from app.llm import llm_provider

        # 1. LLM extracts structured API spec
        payload = await llm_provider.chat_structured(
            response_model=RegisterApiToolInput,
            messages=[
                {"role": "system", "content": _REGISTRATION_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0.0,
        )
        if not payload:
            return {
                "success": False,
                "error": "Не удалось извлечь описание API. Укажите URL, параметры и название.",
            }

        # 2. Validate tool name
        name = payload.tool_name.strip().lower().replace("-", "_").replace(" ", "_")
        if not _SAFE_NAME_RE.match(name):
            return {
                "success": False,
                "error": f"Некорректное имя инструмента: '{name}'. Используйте латиницу, snake_case, 2-63 символа.",
            }

        # 3. SSRF-safe URL validation
        try:
            egress_policy_service.validate_url(payload.api_endpoint)
        except ValueError as exc:
            return {
                "success": False,
                "error": f"URL заблокирован политикой безопасности: {exc}",
            }

        # 4. Encrypt auth data
        from app.services.auth_data_security_service import auth_data_security_service
        auth_data: dict = {}
        if payload.auth_token:
            auth_data = auth_data_security_service.encrypt({"token": payload.auth_token})

        # 5. Persist in PostgreSQL (DynamicTool table)
        from app.models.dynamic_tool import DynamicTool
        from sqlalchemy import select

        existing_result = await db.execute(
            select(DynamicTool).where(
                DynamicTool.user_id == user_id,
                DynamicTool.name == name,
            )
        )
        existing = existing_result.scalar_one_or_none()

        if existing:
            existing.description = payload.description
            existing.endpoint = payload.api_endpoint
            existing.method = (payload.method or "GET").upper()
            existing.headers = payload.headers or {}
            existing.auth_data = auth_data
            existing.parameters_schema = payload.parameters_schema or {}
            existing.response_hint = payload.response_hint or ""
            existing.is_active = True
            db.add(existing)
            status = "updated"
        else:
            tool = DynamicTool(
                user_id=user_id,
                name=name,
                description=payload.description,
                endpoint=payload.api_endpoint,
                method=(payload.method or "GET").upper(),
                headers=payload.headers or {},
                auth_data=auth_data,
                parameters_schema=payload.parameters_schema or {},
                response_hint=payload.response_hint or "",
                is_active=True,
            )
            db.add(tool)
            status = "registered"

        await db.commit()

        # 6. Save to Milvus vector store for semantic retrieval
        try:
            await vector_tool_registry.register_tool(
                user_id=str(user_id),
                tool_name=f"dyn:{name}",
                tool_type="dynamic",
                description=payload.description,
                endpoint=payload.api_endpoint,
                method=(payload.method or "GET").upper(),
                parameters_schema=payload.parameters_schema or {},
                metadata={
                    "response_hint": payload.response_hint or "",
                    "headers": payload.headers or {},
                },
            )
        except Exception as exc:
            logger.warning("Failed to save tool vector for %s: %s", name, exc)
            # Non-fatal — tool is still usable via DB, just won't be found by semantic search

        logger.info(
            "register_api_tool %s: %s (user=%s, endpoint=%s)",
            status, name, user_id, payload.api_endpoint,
        )

        return {
            "success": True,
            "status": status,
            "tool": {
                "name": name,
                "description": payload.description,
                "endpoint": payload.api_endpoint,
                "method": (payload.method or "GET").upper(),
                "parameters_schema": payload.parameters_schema or {},
            },
            "message": (
                f"Инструмент **{name}** {'обновлён' if status == 'updated' else 'зарегистрирован'}. "
                f"Эндпоинт: {payload.api_endpoint}. "
                f"Теперь я смогу находить его семантически по вашим запросам."
            ),
        }


register_api_tool_service = RegisterApiToolService()
