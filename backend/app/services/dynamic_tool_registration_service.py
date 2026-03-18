"""Dynamic Tool Registration Service — NL-assisted and ZIP-based registration.

Handles tool registration via two channels:
1. Natural Language: user describes API → LLM extracts spec → register
2. ZIP Package: user provides skill package → validate → register
"""

import re
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.dynamic_tool_registration_helpers import (
    register_from_user_message as register_nl,
    register_skill_package as register_zip,
)

logger = None  # Lazy import in async methods

# Allowed characters in tool names (prevents LLM injection)
SAFE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")

# Meta-tool system prompt for NL registration
META_REGISTRATION_PROMPT = """\
Ты — продвинутый ИИ-оркестратор. Твоя задача: помогать пользователю подключать новые API к твоему функционалу.

### ТВОИ ВОЗМОЖНОСТИ:
1. Если пользователь предоставляет данные об API (URL, параметры, описание), ты должен вернуть JSON для регистрации инструмента.
2. Ты должен проанализировать описание и создать валидную JSON Schema для параметров этого API.
3. Если данных недостаточно (например, нет URL), ты должен вежливо уточнить.

### ПРАВИЛА СОЗДАНИЯ СХЕМЫ:
- Верни строго JSON без markdown.
- Формат: {"tool_name": "...", "description": "...", "api_endpoint": "...", "method": "GET|POST", "headers": {}, "auth_token": null, "parameters_schema": {"type": "object", "properties": {...}, "required": [...]}, "response_hint": "..."}
- Для каждого параметра добавляй `description`.
- Угадывай типы данных (string, number, boolean) на основе описания.
- tool_name: латиница, snake_case, без пробелов, макс 63 символа.
- Если пользователь упоминает токен, положи его в auth_token.
- Если пользователь упоминает headers (например Content-Type), положи их в headers.

### ПРИМЕР:
Пользователь: "Подключи API погоды https://api.weather.com/v1, нужен параметр city (строка)".
Ответ: {"tool_name": "weather_api", "description": "Получение прогноза погоды", "api_endpoint": "https://api.weather.com/v1", "method": "GET", "headers": {}, "auth_token": null, "parameters_schema": {"type": "object", "properties": {"city": {"type": "string", "description": "Город"}}, "required": ["city"]}, "response_hint": "JSON с данными о погоде"}
"""


def _normalize_tool_name(name: str) -> str:
    """Normalize tool name to snake_case."""
    return str(name or "").removeprefix("dyn:").removeprefix("dyn_").strip().lower().replace("-", "_").replace(" ", "_")


def _candidate_tool_names(name: str) -> list[str]:
    """Generate candidate tool names for lookup (with/without python:// prefix)."""
    normalized = _normalize_tool_name(name)
    if not normalized:
        return []
    candidates = [normalized]
    if normalized.startswith("python://"):
        candidates.append(_normalize_tool_name(normalized.removeprefix("python://")))
    else:
        candidates.append(f"python://{normalized}")
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


class DynamicToolRegistrationService:
    """Register dynamic tools via natural language or ZIP packages."""

    async def register_from_user_message(
        self,
        db: AsyncSession,
        user_id: UUID,
        user_message: str,
    ) -> dict:
        """Parse NL API description via LLM and register tool.

        User describes API in natural language. LLM (via META_REGISTRATION_PROMPT)
        extracts JSON spec. Service validates and persists as DynamicTool.

        Args:
            db: Database session
            user_id: User identifier
            user_message: Natural language API description

        Returns:
            Dict with:
            - status: 'ok', 'error', or 'needs_clarification'
            - tool: DynamicTool-like dict if status='ok'
            - message: User-facing response
        """
        return await register_nl(
            self,
            db=db,
            user_id=user_id,
            user_message=user_message,
            meta_registration_prompt=META_REGISTRATION_PROMPT,
            safe_name_pattern=SAFE_NAME_RE,
        )

    async def register_skill_package(
        self,
        db: AsyncSession,
        user_id: UUID,
        *,
        filename: str,
        content: bytes,
    ) -> dict:
        """Register Python Dynamic Skill from add_skill.zip.

        Validates ZIP structure, code, manifest. Stores package.
        Returns registration result.

        Args:
            db: Database session
            user_id: User identifier
            filename: ZIP filename (for logging)
            content: ZIP file bytes

        Returns:
            Dict with:
            - status: 'ok' or 'error'
            - tool: DynamicTool-like dict if status='ok'
            - message: User-facing response or error description
        """
        return await register_zip(
            self,
            db=db,
            user_id=user_id,
            filename=filename,
            content=content,
            safe_name_pattern=SAFE_NAME_RE,
            max_zip_bytes=2 * 1024 * 1024,  # 2 MB
        )


dynamic_tool_registration_service = DynamicToolRegistrationService()
