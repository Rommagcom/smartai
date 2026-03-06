"""Dynamic Tool Service — register, discover, and call user-defined API tools.

Implements the *Dynamic Tool Injection* pattern:
1. **Registration** — user describes an API in natural language, LLM generates
   ``ApiRegistrationPayload``, the service persists it as ``DynamicTool``.
2. **Discovery** — ``get_tools_for_planner()`` returns planner-compatible
   signatures for all active tools of a user so the LLM can decide to call them.
3. **Execution** — ``call_dynamic_tool()`` performs the real HTTP request using
   the stored endpoint/method/auth.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.dynamic_tool import DynamicTool
from app.services.api_executor import api_executor, resolve_url_template
from app.services.auth_data_security_service import auth_data_security_service
from app.services.egress_policy_service import egress_policy_service

logger = logging.getLogger(__name__)

# Allowed characters in tool names (prevents LLM injection in tool names)
_SAFE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")

# Meta-tool system prompt that teaches the LLM to extract API specs from speech
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


class DynamicToolService:
    """CRUD + LLM-assisted registration + runtime invocation of dynamic tools."""

    # ------------------------------------------------------------------ #
    # Registration via LLM (meta-tool)
    # ------------------------------------------------------------------ #

    async def register_from_user_message(
        self,
        db: AsyncSession,
        user_id: UUID,
        user_message: str,
    ) -> dict:
        """Parse user's natural-language API description via LLM and persist.

        Returns a dict with ``status``, ``tool`` (DynamicToolOut-like), and
        ``message`` for the final answer composer.
        """
        from app.llm import llm_provider
        from app.schemas.dynamic_tool import ApiRegistrationPayload

        # Ask LLM to extract structured API spec from the user's message
        payload = await llm_provider.chat_structured(
            response_model=ApiRegistrationPayload,
            messages=[
                {"role": "system", "content": META_REGISTRATION_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0.0,
        )

        if not payload:
            return {
                "status": "failed",
                "message": "Не удалось извлечь описание API из сообщения. Укажите URL, параметры и название.",
            }

        # Validate tool name
        name = payload.tool_name.strip().lower().replace("-", "_").replace(" ", "_")
        if not _SAFE_NAME_RE.match(name):
            return {
                "status": "failed",
                "message": f"Некорректное имя инструмента: '{name}'. Используйте латиницу, snake_case, 2-63 символа.",
            }

        # Validate endpoint URL (egress policy)
        try:
            egress_policy_service.validate_url(payload.api_endpoint)
        except ValueError as exc:
            return {
                "status": "failed",
                "message": f"URL заблокирован политикой безопасности: {exc}",
            }

        # Encrypt auth data if token provided
        auth_data: dict = {}
        if payload.auth_token:
            auth_data = auth_data_security_service.encrypt({"token": payload.auth_token})

        # Upsert — update if tool with same name exists for this user
        existing = await self._get_by_name(db, user_id, name)
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
            await db.commit()
            await db.refresh(existing)
            tool = existing
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
            await db.commit()
            await db.refresh(tool)
            status = "registered"

        logger.info(
            "dynamic tool %s: %s (user=%s, endpoint=%s)",
            status,
            name,
            user_id,
            payload.api_endpoint,
        )

        # Sync to Milvus for semantic retrieval
        try:
            from app.services.vector_tool_registry import vector_tool_registry
            await vector_tool_registry.register_tool(
                user_id=str(user_id),
                tool_name=f"dyn:{name}",
                tool_type="dynamic",
                description=payload.description,
                endpoint=payload.api_endpoint,
                method=(payload.method or "GET").upper(),
                parameters_schema=payload.parameters_schema or {},
                metadata={"response_hint": payload.response_hint or ""},
            )
        except Exception as exc:
            logger.debug("failed to sync dynamic tool to Milvus: %s", exc)

        return {
            "status": status,
            "tool": {
                "id": str(tool.id),
                "name": tool.name,
                "description": tool.description,
                "endpoint": tool.endpoint,
                "method": tool.method,
                "parameters_schema": tool.parameters_schema,
            },
            "message": (
                f"Инструмент **{name}** {'обновлён' if status == 'updated' else 'зарегистрирован'}. "
                f"Теперь я могу использовать его для запросов к {payload.api_endpoint}."
            ),
        }

    # ------------------------------------------------------------------ #
    # Manual CRUD
    # ------------------------------------------------------------------ #

    async def create_tool(
        self,
        db: AsyncSession,
        user_id: UUID,
        *,
        name: str,
        description: str = "",
        endpoint: str,
        method: str = "GET",
        headers: dict | None = None,
        auth_token: str | None = None,
        parameters_schema: dict | None = None,
        response_hint: str = "",
    ) -> DynamicTool:
        name = name.strip().lower().replace("-", "_").replace(" ", "_")
        if not _SAFE_NAME_RE.match(name):
            raise ValueError(f"Invalid tool name: {name}")

        egress_policy_service.validate_url(endpoint)

        auth_data: dict = {}
        if auth_token:
            auth_data = auth_data_security_service.encrypt({"token": auth_token})

        tool = DynamicTool(
            user_id=user_id,
            name=name,
            description=description,
            endpoint=endpoint,
            method=method.upper(),
            headers=headers or {},
            auth_data=auth_data,
            parameters_schema=parameters_schema or {},
            response_hint=response_hint,
            is_active=True,
        )
        db.add(tool)
        await db.flush()
        return tool

    async def list_tools(
        self,
        db: AsyncSession,
        user_id: UUID,
        active_only: bool = True,
    ) -> list[DynamicTool]:
        q = select(DynamicTool).where(DynamicTool.user_id == user_id)
        if active_only:
            q = q.where(DynamicTool.is_active.is_(True))
        q = q.order_by(DynamicTool.created_at.desc())
        result = await db.execute(q)
        return list(result.scalars().all())

    async def delete_tool(
        self,
        db: AsyncSession,
        user_id: UUID,
        tool_id: UUID,
    ) -> bool:
        result = await db.execute(
            select(DynamicTool).where(
                DynamicTool.id == tool_id,
                DynamicTool.user_id == user_id,
            )
        )
        tool = result.scalar_one_or_none()
        if not tool:
            return False
        tool_name = tool.name
        await db.delete(tool)
        await db.commit()
        # Remove from Milvus
        try:
            from app.services.vector_tool_registry import vector_tool_registry
            vector_tool_registry.delete_tool(user_id=str(user_id), tool_name=f"dyn:{tool_name}")
        except Exception as exc:
            logger.debug("failed to delete tool vector: %s", exc)
        return True

    async def delete_all_tools(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> int:
        result = await db.execute(
            select(DynamicTool).where(DynamicTool.user_id == user_id)
        )
        tools = result.scalars().all()
        for t in tools:
            await db.delete(t)
        await db.commit()
        # Remove all from Milvus
        try:
            from app.services.vector_tool_registry import vector_tool_registry
            vector_tool_registry.delete_user_tools(user_id=str(user_id))
        except Exception as exc:
            logger.debug("failed to delete user tool vectors: %s", exc)
        return len(tools)

    # ------------------------------------------------------------------ #
    # Discovery — inject into planner context
    # ------------------------------------------------------------------ #

    async def get_tools_for_planner(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> str:
        """Return a planner-compatible signature block for all active dynamic tools.

        Format matches ``skills_registry_service.planner_signatures()``::

            dyn:weather_api(city) — Получение прогноза погоды,
            dyn:check_order(order_id) — Проверка заказа в CRM
        """
        tools = await self.list_tools(db, user_id, active_only=True)
        if not tools:
            return ""

        parts: list[str] = []
        for t in tools:
            params = self._extract_param_names(t.parameters_schema)
            sig = f"dyn:{t.name}({', '.join(params)})" if params else f"dyn:{t.name}()"
            desc = t.description[:80] if t.description else ""
            parts.append(f"{sig} — {desc}" if desc else sig)
        return ", ".join(parts)

    async def get_tools_for_llm(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> list[dict]:
        """Return tool definitions in OpenAI function-calling format.

        These can be passed to LiteLLM's ``tools`` parameter so the LLM
        can directly call dynamic tools via tool_calls.
        """
        tools = await self.list_tools(db, user_id, active_only=True)
        result: list[dict] = []
        for t in tools:
            schema = t.parameters_schema if isinstance(t.parameters_schema, dict) else {}
            if not schema.get("type"):
                schema = {"type": "object", "properties": schema}
            result.append({
                "type": "function",
                "function": {
                    "name": f"dyn_{t.name}",
                    "description": t.description or f"Пользовательский API: {t.name}",
                    "parameters": schema,
                },
            })
        return result

    # ------------------------------------------------------------------ #
    # Execution — call the registered API
    # ------------------------------------------------------------------ #

    async def call_dynamic_tool(
        self,
        db: AsyncSession,
        user_id: UUID,
        tool_name: str,
        arguments: dict,
    ) -> dict:
        """Execute a dynamic tool by name.

        Performs the HTTP request to the registered endpoint with the given
        arguments as query params (GET) or JSON body (POST/PUT/PATCH).
        """
        # Strip dyn: prefix if present
        clean_name = tool_name.removeprefix("dyn:").removeprefix("dyn_").strip().lower()

        tool = await self._get_by_name(db, user_id, clean_name)
        if not tool:
            return {"success": False, "error": f"Dynamic tool '{clean_name}' not found"}

        if not tool.is_active:
            return {"success": False, "error": f"Dynamic tool '{clean_name}' is disabled"}

        # Resolve auth
        headers = dict(tool.headers) if tool.headers else {}
        auth_data_raw = tool.auth_data or {}
        if auth_data_raw:
            auth_data, _ = auth_data_security_service.resolve_for_runtime(auth_data_raw)
            if token := auth_data.get("token"):
                headers["Authorization"] = f"Bearer {token}"

        # Build URL with params
        url = resolve_url_template(tool.endpoint, arguments if tool.method == "GET" else {})

        # Execute
        method = tool.method or "GET"
        body = arguments if method in ("POST", "PUT", "PATCH") else None

        try:
            async with asyncio.timeout(30):
                result = await api_executor.call(
                    method=method,
                    url=url,
                    headers=headers,
                    body=body,
                )

            # Try to parse JSON body
            body_text = result.get("body", "")
            try:
                parsed_body = json.loads(body_text)
            except (json.JSONDecodeError, TypeError):
                parsed_body = body_text

            return {
                "success": result.get("status_code", 0) < 400,
                "status_code": result.get("status_code"),
                "data": parsed_body,
                "tool_name": clean_name,
                "endpoint": tool.endpoint,
            }
        except asyncio.TimeoutError:
            return {"success": False, "error": f"Timeout calling {tool.endpoint}"}
        except Exception as exc:
            logger.warning("dynamic tool call failed: %s — %s", clean_name, exc)
            return {"success": False, "error": str(exc)}

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    async def _get_by_name(
        db: AsyncSession,
        user_id: UUID,
        name: str,
    ) -> DynamicTool | None:
        result = await db.execute(
            select(DynamicTool).where(
                DynamicTool.user_id == user_id,
                DynamicTool.name == name,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _extract_param_names(schema: dict) -> list[str]:
        """Extract parameter names from a JSON Schema properties dict."""
        if not isinstance(schema, dict):
            return []
        properties = schema.get("properties")
        if isinstance(properties, dict):
            return list(properties.keys())
        # Fallback: if schema IS the properties dict directly
        if schema and "type" not in schema:
            return list(schema.keys())
        return []


dynamic_tool_service = DynamicToolService()
