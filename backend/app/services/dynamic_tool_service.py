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

import io
import json
import logging
import re
import shutil
import zipfile
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.dynamic_tool import DynamicTool
from app.models.user import User
from app.services.auth_data_security_service import auth_data_security_service
from app.services.dynamic_python_skill_support import (
    SKILL_CODE_FILE,
    SKILL_MANIFEST_FILE,
    SKILL_README_FILE,
    build_runner_context,
    dynamic_skill_storage_root,
    extract_description_from_md,
    store_skill_package,
    validate_python_skill_code,
)
from app.services.dynamic_skill_audit_service import dynamic_skill_audit_service
from app.services.egress_policy_service import egress_policy_service

logger = logging.getLogger(__name__)

# Allowed characters in tool names (prevents LLM injection in tool names)
_SAFE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
_DYNAMIC_SKILL_MAX_ZIP_BYTES = 2 * 1024 * 1024
_DYNAMIC_SKILL_REQUIRED_FILES = {SKILL_MANIFEST_FILE, SKILL_CODE_FILE, SKILL_README_FILE}
def _normalize_tool_name(name: str) -> str:
    return str(name or "").removeprefix("dyn:").removeprefix("dyn_").strip().lower().replace("-", "_").replace(" ", "_")


def _candidate_tool_names(name: str) -> list[str]:
    normalized = _normalize_tool_name(name)
    if not normalized:
        return []
    candidates = [normalized]
    if normalized.startswith("python://"):
        candidates.append(_normalize_tool_name(normalized.removeprefix("python://")))
    else:
        candidates.append(f"python://{normalized}")
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))

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

    @staticmethod
    def _python_skill_filter():
        return or_(
            func.lower(DynamicTool.method) == "python",
            func.lower(DynamicTool.endpoint).like("python://%"),
        )

    @classmethod
    def _apply_kind_filter(cls, query: Any, kind: str | None) -> Any:
        if kind == "python_skill":
            return query.where(cls._python_skill_filter())
        if kind == "api_tool":
            return query.where(~cls._python_skill_filter())
        return query

    @staticmethod
    def _log_skill_execution_event(
        *,
        event: str,
        user_id: UUID,
        tool_name: str,
        execution_mode: str,
        success: bool | None = None,
        error: str | None = None,
    ) -> None:
        context: dict[str, Any] = {
            "component": "dynamic_skill",
            "event": event,
            "user_id": str(user_id),
            "tool_name": tool_name,
            "execution_mode": execution_mode,
        }
        if success is not None:
            context["success"] = success
        if error:
            context["error"] = error[:500]
        logger.info("dynamic skill execution event", extra={"context": context})

    @staticmethod
    async def _persist_skill_execution_event(
        *,
        event: str,
        user_id: UUID,
        tool_name: str,
        execution_mode: str,
        execution_id: str,
        success: bool | None = None,
        error: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        try:
            await dynamic_skill_audit_service.record_event(
                event_type=event,
                source="backend",
                execution_mode=execution_mode,
                tool_name=tool_name,
                execution_id=execution_id,
                user_id=user_id,
                success=success,
                error_text=error or "",
                payload=payload,
            )
        except Exception as exc:
            logger.warning("dynamic skill audit persistence failed: %s", exc)

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
        from app.services.dynamic_tool_registration_helpers import register_from_user_message

        return await register_from_user_message(
            self,
            db=db,
            user_id=user_id,
            user_message=user_message,
            meta_registration_prompt=META_REGISTRATION_PROMPT,
            safe_name_pattern=_SAFE_NAME_RE,
        )

    async def register_skill_package(
        self,
        db: AsyncSession,
        user_id: UUID,
        *,
        filename: str,
        content: bytes,
    ) -> dict:
        """Register a Python Dynamic Skill from add_skill.zip payload."""
        from app.services.dynamic_tool_registration_helpers import register_skill_package

        return await register_skill_package(
            self,
            db=db,
            user_id=user_id,
            filename=filename,
            content=content,
            safe_name_pattern=_SAFE_NAME_RE,
            max_zip_bytes=_DYNAMIC_SKILL_MAX_ZIP_BYTES,
        )

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
        kind: str | None = None,
    ) -> list[DynamicTool]:
        q = select(DynamicTool).where(DynamicTool.user_id == user_id)
        if active_only:
            q = q.where(DynamicTool.is_active.is_(True))
        q = self._apply_kind_filter(q, kind)
        q = q.order_by(DynamicTool.created_at.desc())
        result = await db.execute(q)
        return list(result.scalars().all())

    async def delete_tool(
        self,
        db: AsyncSession,
        user_id: UUID,
        tool_id: UUID,
    ) -> bool:
        user_row = (
            await db.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
        if not user_row or not bool(getattr(user_row, "is_admin", False)):
            return False

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
        self._cleanup_tool_storage(tool)
        await db.delete(tool)
        await db.commit()
        # Remove from Milvus
        try:
            from app.services.vector_tool_registry import vector_tool_registry
            vector_tool_registry.delete_tool(user_id=str(user_id), tool_name=f"dyn:{tool_name}")
        except Exception as exc:
            logger.debug("failed to delete tool vector: %s", exc)
        return True

    async def delete_tool_by_name(
        self,
        db: AsyncSession,
        user_id: UUID,
        tool_name: str,
        kind: str | None = None,
    ) -> bool:
        clean_name = _normalize_tool_name(tool_name)
        if not clean_name:
            return False

        tool = await self._get_by_name(db, user_id, clean_name, kind=kind)
        if not tool:
            return False
        return await self.delete_tool(db=db, user_id=user_id, tool_id=tool.id)

    async def delete_all_tools(
        self,
        db: AsyncSession,
        user_id: UUID,
        kind: str | None = None,
    ) -> int:
        user_row = (
            await db.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
        if not user_row or not bool(getattr(user_row, "is_admin", False)):
            return 0

        query = select(DynamicTool).where(DynamicTool.user_id == user_id)
        query = self._apply_kind_filter(query, kind)
        result = await db.execute(query)
        tools = result.scalars().all()
        for t in tools:
            self._cleanup_tool_storage(t)
            await db.delete(t)
        await db.commit()
        # Remove all from Milvus
        try:
            from app.services.vector_tool_registry import vector_tool_registry
            if kind is None:
                vector_tool_registry.delete_user_tools(user_id=str(user_id))
            else:
                for tool in tools:
                    vector_tool_registry.delete_tool(user_id=str(user_id), tool_name=f"dyn:{tool.name}")
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

        Format matches ``tool_catalog_service.planner_signatures()``::

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
        from app.services.dynamic_python_skill_runtime import call_dynamic_tool

        return await call_dynamic_tool(
            self,
            db=db,
            user_id=user_id,
            tool_name=tool_name,
            arguments=arguments,
        )

    async def _call_python_skill(self, *, user_id: UUID, tool: DynamicTool, arguments: dict) -> dict:
        from app.services.dynamic_python_skill_runtime import call_python_skill

        return await call_python_skill(self, user_id=user_id, tool=tool, arguments=arguments)

    @staticmethod
    def _user_select(user_id: UUID):
        return select(User).where(User.id == user_id)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    async def _get_by_name(
        db: AsyncSession,
        user_id: UUID,
        name: str,
        kind: str | None = None,
    ) -> DynamicTool | None:
        candidates = _candidate_tool_names(name)
        if not candidates:
            return None
        query = select(DynamicTool).where(
            DynamicTool.user_id == user_id,
            or_(
                func.lower(DynamicTool.name).in_(candidates),
                func.lower(DynamicTool.endpoint).in_(candidates),
            ),
        )
        query = DynamicToolService._apply_kind_filter(query, kind)
        result = await db.execute(query)
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

    @staticmethod
    def _cleanup_tool_storage(tool: DynamicTool) -> None:
        auth_data = tool.auth_data if isinstance(tool.auth_data, dict) else {}
        storage_dir = str(auth_data.get("storage_dir") or "").strip()
        if not storage_dir:
            return
        path = Path(storage_dir)
        try:
            if path.exists() and path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
        except Exception:
            logger.debug("failed to cleanup dynamic skill storage: %s", storage_dir)

    @staticmethod
    def _parse_skill_zip(content: bytes) -> dict[str, Any]:
        try:
            zf = zipfile.ZipFile(io.BytesIO(content))
        except Exception as exc:
            raise ValueError(f"zip parse failed: {exc}") from exc

        with zf:
            names = [str(n or "") for n in zf.namelist()]
            files = {name for name in names if not name.endswith("/")}
            for name in files:
                normalized = name.replace("\\", "/")
                if normalized.startswith("/") or ".." in normalized.split("/"):
                    raise ValueError(f"unsafe zip path: {name}")

            flat_names = {Path(name).name for name in files}
            missing = [required for required in _DYNAMIC_SKILL_REQUIRED_FILES if required not in flat_names]
            if missing:
                raise ValueError(f"missing required files: {', '.join(sorted(missing))}")

            manifest_name = next(name for name in files if Path(name).name == "manifest.json")
            skill_name = next(name for name in files if Path(name).name == "skill.py")
            md_name = next(name for name in files if Path(name).name == "skill.md")

            manifest_raw = zf.read(manifest_name).decode("utf-8")
            skill_py = zf.read(skill_name).decode("utf-8")
            skill_md = zf.read(md_name).decode("utf-8")

        try:
            manifest = json.loads(manifest_raw)
        except Exception as exc:
            raise ValueError(f"manifest.json is not valid JSON: {exc}") from exc

        if not isinstance(manifest, dict):
            raise ValueError("manifest.json must be an object")

        DynamicToolService._validate_skill_manifest(manifest)
        return {
            "manifest": manifest,
            "skill_py": skill_py,
            "skill_md": skill_md,
        }

    @staticmethod
    def _validate_skill_manifest(manifest: dict) -> None:
        # entrypoint and function are optional — register_skill_package defaults
        # to "skill.py" and "run" when they are absent, matching the README example.
        required = ["name", "input_schema"]
        missing = [key for key in required if key not in manifest]
        if missing:
            raise ValueError(f"manifest missing required fields: {', '.join(missing)}")

        input_schema = manifest.get("input_schema")
        if not isinstance(input_schema, dict) or str(input_schema.get("type") or "") != "object":
            raise ValueError("manifest.input_schema must be JSON Schema object with type='object'")

        capabilities = manifest.get("capabilities")
        if capabilities is not None and not isinstance(capabilities, dict):
            raise ValueError("manifest.capabilities must be an object when provided")

    @staticmethod
    def _validate_python_skill_code(*, skill_code: str, function_name: str) -> None:
        validate_python_skill_code(skill_code=skill_code, function_name=function_name)

    @staticmethod
    def _extract_description_from_md(skill_md: str) -> str:
        return extract_description_from_md(skill_md)

    @staticmethod
    def _dynamic_skill_storage_root() -> Path:
        return dynamic_skill_storage_root()

    @classmethod
    def _store_skill_package(
        cls,
        *,
        user_id: UUID,
        name: str,
        version: str,
        skill_code: str,
        skill_md: str,
        manifest: dict,
        zip_payload: bytes,
    ) -> dict[str, str]:
        return store_skill_package(
            user_id=user_id,
            name=name,
            version=version,
            skill_code=skill_code,
            skill_md=skill_md,
            manifest=manifest,
            zip_payload=zip_payload,
        )

    def _build_python_skill_context(self, *, user_id: UUID, tool_name: str, capabilities: dict) -> dict:
        return build_python_skill_context(user_id=user_id, tool_name=tool_name, capabilities=capabilities)

    @staticmethod
    def _build_runner_context(*, user_id: UUID, tool_name: str, capabilities: dict, execution_id: str) -> dict[str, Any]:
        return build_runner_context(
            user_id=user_id,
            tool_name=tool_name,
            capabilities=capabilities,
            execution_id=execution_id,
        )


dynamic_tool_service = DynamicToolService()
