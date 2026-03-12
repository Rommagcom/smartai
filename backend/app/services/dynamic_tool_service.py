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

import ast
import asyncio
import hashlib
import io
import json
import logging
from pathlib import Path
import re
import shutil
import zipfile
from typing import Any
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
_DYNAMIC_SKILL_MAX_ZIP_BYTES = 2 * 1024 * 1024
_DYNAMIC_SKILL_REQUIRED_FILES = {"manifest.json", "skill.py", "skill.md"}
_DYNAMIC_SKILL_ALLOWED_IMPORTS = {"math", "json", "re", "datetime", "statistics", "typing"}

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

    async def register_skill_package(
        self,
        db: AsyncSession,
        user_id: UUID,
        *,
        filename: str,
        content: bytes,
    ) -> dict:
        """Register a Python Dynamic Skill from add_skill.zip payload."""
        safe_filename = str(filename or "add_skill.zip").strip() or "add_skill.zip"
        if not safe_filename.lower().endswith(".zip"):
            return {"status": "failed", "message": "Skill package must be a .zip archive."}

        if not isinstance(content, (bytes, bytearray)) or not content:
            return {"status": "failed", "message": "Skill package is empty."}

        if len(content) > _DYNAMIC_SKILL_MAX_ZIP_BYTES:
            return {
                "status": "failed",
                "message": f"Skill package is too large ({len(content)} bytes). Max: {_DYNAMIC_SKILL_MAX_ZIP_BYTES} bytes.",
            }

        try:
            parsed = self._parse_skill_zip(content)
        except ValueError as exc:
            return {"status": "failed", "message": f"Invalid skill package: {exc}"}

        manifest = parsed["manifest"]
        skill_code = parsed["skill_py"]
        skill_md = parsed["skill_md"]

        name = str(manifest.get("name") or "").strip().lower()
        if not _SAFE_NAME_RE.match(name):
            return {
                "status": "failed",
                "message": f"Invalid skill name '{name}'. Use latin snake_case, 2-63 chars.",
            }

        entrypoint = str(manifest.get("entrypoint") or "skill.py").strip() or "skill.py"
        function_name = str(manifest.get("function") or "run").strip() or "run"
        description = str(manifest.get("description") or "").strip() or self._extract_description_from_md(skill_md)
        parameters_schema = manifest.get("input_schema") if isinstance(manifest.get("input_schema"), dict) else {}
        capabilities = manifest.get("capabilities") if isinstance(manifest.get("capabilities"), dict) else {}

        # Dry-run compile + contract validation before persisting.
        try:
            self._validate_python_skill_code(skill_code=skill_code, function_name=function_name)
        except ValueError as exc:
            return {"status": "failed", "message": f"Skill code validation failed: {exc}"}

        storage_meta = self._store_skill_package(
            user_id=user_id,
            name=name,
            version=str(manifest.get("version") or "1.0.0"),
            skill_code=skill_code,
            skill_md=skill_md,
            manifest=manifest,
            zip_payload=bytes(content),
        )

        existing = await self._get_by_name(db, user_id, name)
        method = "PYTHON"
        endpoint = f"python://{name}"
        headers = {
            "skill_type": "python_zip",
            "entrypoint": entrypoint,
            "function": function_name,
            "capabilities": capabilities,
            "manifest_version": str(manifest.get("version") or "1.0.0"),
        }
        auth_data = {
            "storage_dir": storage_meta["storage_dir"],
            "manifest_path": storage_meta["manifest_path"],
            "skill_path": storage_meta["skill_path"],
            "readme_path": storage_meta["readme_path"],
            "zip_sha256": storage_meta["zip_sha256"],
        }

        if existing:
            existing.description = description
            existing.endpoint = endpoint
            existing.method = method
            existing.headers = headers
            existing.auth_data = auth_data
            existing.parameters_schema = parameters_schema
            existing.response_hint = str(manifest.get("response_hint") or "")
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
                description=description,
                endpoint=endpoint,
                method=method,
                headers=headers,
                auth_data=auth_data,
                parameters_schema=parameters_schema,
                response_hint=str(manifest.get("response_hint") or ""),
                is_active=True,
            )
            db.add(tool)
            await db.commit()
            await db.refresh(tool)
            status = "registered"

        try:
            from app.services.vector_tool_registry import vector_tool_registry

            await vector_tool_registry.register_tool(
                user_id=str(user_id),
                tool_name=f"dyn:{name}",
                tool_type="dynamic_python_skill",
                description=description,
                endpoint=endpoint,
                method=method,
                parameters_schema=parameters_schema,
                metadata={"capabilities": capabilities, "manifest_version": str(manifest.get("version") or "1.0.0")},
            )
        except Exception as exc:
            logger.debug("failed to sync dynamic python skill to Milvus: %s", exc)

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
            "message": f"Dynamic Skill '{name}' {'updated' if status == 'updated' else 'registered'}.",
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
            self._cleanup_tool_storage(t)
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

        method = (tool.method or "GET").upper()
        endpoint = str(tool.endpoint or "").strip()
        if method == "PYTHON" or endpoint.startswith("python://"):
            return await self._call_python_skill(user_id=user_id, tool=tool, arguments=arguments)

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

    async def _call_python_skill(self, *, user_id: UUID, tool: DynamicTool, arguments: dict) -> dict:
        auth_data = tool.auth_data if isinstance(tool.auth_data, dict) else {}
        headers = tool.headers if isinstance(tool.headers, dict) else {}
        skill_path = str(auth_data.get("skill_path") or "").strip()
        if not skill_path:
            return {"success": False, "error": "Dynamic Python Skill is not installed correctly (missing skill_path)."}

        path_obj = Path(skill_path)
        if not path_obj.exists() or not path_obj.is_file():
            return {"success": False, "error": "Dynamic Python Skill code file is missing."}

        try:
            skill_code = path_obj.read_text(encoding="utf-8")
        except Exception as exc:
            return {"success": False, "error": f"Failed to read skill code: {exc}"}

        function_name = str(headers.get("function") or "run").strip() or "run"
        capabilities = headers.get("capabilities") if isinstance(headers.get("capabilities"), dict) else {}

        try:
            self._validate_python_skill_code(skill_code=skill_code, function_name=function_name)
        except ValueError as exc:
            return {"success": False, "error": f"Skill validation failed: {exc}"}

        runtime_context = self._build_python_skill_context(
            user_id=user_id,
            tool_name=str(tool.name),
            capabilities=capabilities,
        )

        timeout_seconds = 30
        llm_caps = capabilities.get("llm") if isinstance(capabilities, dict) else None
        if isinstance(llm_caps, dict):
            timeout_candidate = llm_caps.get("timeout_seconds")
            if isinstance(timeout_candidate, int) and 1 <= timeout_candidate <= 60:
                timeout_seconds = timeout_candidate

        try:
            call_result = await asyncio.wait_for(
                asyncio.to_thread(
                    self._execute_python_skill_sync,
                    skill_code,
                    function_name,
                    dict(arguments or {}),
                    runtime_context,
                ),
                timeout=timeout_seconds,
            )
            if isinstance(call_result, dict):
                data = call_result
            else:
                data = {"result": call_result}
            return {"success": True, "tool_name": str(tool.name), "data": data}
        except asyncio.TimeoutError:
            return {"success": False, "error": f"Dynamic Python Skill timeout after {timeout_seconds}s"}
        except Exception as exc:
            logger.warning("dynamic python skill execution failed: %s — %s", tool.name, exc)
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
        return {"manifest": manifest, "skill_py": skill_py, "skill_md": skill_md}

    @staticmethod
    def _validate_skill_manifest(manifest: dict) -> None:
        required = ["name", "entrypoint", "function", "input_schema"]
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
        try:
            module_ast = ast.parse(skill_code, filename="skill.py")
        except SyntaxError as exc:
            raise ValueError(f"syntax error: {exc}") from exc

        has_function = False
        for node in ast.walk(module_ast):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module_name = ""
                if isinstance(node, ast.Import):
                    if node.names:
                        module_name = str(node.names[0].name or "")
                elif isinstance(node, ast.ImportFrom):
                    module_name = str(node.module or "")
                root = module_name.split(".")[0]
                if root and root not in _DYNAMIC_SKILL_ALLOWED_IMPORTS:
                    raise ValueError(f"import '{root}' is not allowed")
            if isinstance(node, ast.FunctionDef) and node.name == function_name:
                has_function = True

        if not has_function:
            raise ValueError(f"function '{function_name}' not found in skill.py")

    @staticmethod
    def _extract_description_from_md(skill_md: str) -> str:
        lines = [line.strip() for line in str(skill_md or "").splitlines() if line.strip()]
        if not lines:
            return "Dynamic Python Skill"
        head = lines[0].lstrip("# ").strip()
        return head[:240] if head else "Dynamic Python Skill"

    @staticmethod
    def _dynamic_skill_storage_root() -> Path:
        backend_root = Path(__file__).resolve().parents[2]
        root = backend_root / "data" / "dynamic_skills"
        root.mkdir(parents=True, exist_ok=True)
        return root

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
        storage_root = cls._dynamic_skill_storage_root()
        safe_version = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(version or "1.0.0"))
        skill_dir = storage_root / str(user_id) / name / safe_version
        skill_dir.mkdir(parents=True, exist_ok=True)

        skill_path = skill_dir / "skill.py"
        readme_path = skill_dir / "skill.md"
        manifest_path = skill_dir / "manifest.json"
        zip_sha256 = hashlib.sha256(zip_payload).hexdigest()

        skill_path.write_text(skill_code, encoding="utf-8")
        readme_path.write_text(skill_md, encoding="utf-8")
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        return {
            "storage_dir": str(skill_dir),
            "skill_path": str(skill_path),
            "readme_path": str(readme_path),
            "manifest_path": str(manifest_path),
            "zip_sha256": zip_sha256,
        }

    def _build_python_skill_context(self, *, user_id: UUID, tool_name: str, capabilities: dict) -> dict:
        context: dict[str, Any] = {
            "user_id": str(user_id),
            "tool_name": tool_name,
            "capabilities": capabilities,
        }

        llm_caps = capabilities.get("llm") if isinstance(capabilities, dict) else None
        if not (isinstance(llm_caps, dict) and bool(llm_caps.get("enabled"))):
            return context

        max_tokens = llm_caps.get("max_tokens")
        max_tokens_int = max_tokens if isinstance(max_tokens, int) and 32 <= max_tokens <= 4096 else 1024
        base_temperature = llm_caps.get("temperature")
        base_temperature_float = (
            float(base_temperature)
            if isinstance(base_temperature, (int, float)) and 0 <= float(base_temperature) <= 1
            else 0.2
        )
        allowed_models = llm_caps.get("allowed_models") if isinstance(llm_caps.get("allowed_models"), list) else []
        selected_model = None
        if allowed_models:
            first_model = str(allowed_models[0] or "").strip()
            if first_model and first_model != "default":
                selected_model = first_model

        def llm_chat(*args: Any, **kwargs: Any) -> dict | str:
            # Support both styles used by skills:
            # 1) llm.chat(system="...", user="...", options={}) -> {"text": "..."}
            # 2) llm.chat("...") -> "..."
            payload_options = kwargs.get("options") if isinstance(kwargs.get("options"), dict) else {}
            simple_prompt_mode = False

            if "system" in kwargs or "user" in kwargs:
                system = str(kwargs.get("system") or "")
                user = str(kwargs.get("user") or "")
                if not user and args:
                    user = str(args[0] or "")
            else:
                if len(args) == 1:
                    system = ""
                    user = str(args[0] or "")
                    simple_prompt_mode = True
                elif len(args) >= 2:
                    system = str(args[0] or "")
                    user = str(args[1] or "")
                    if len(args) >= 3 and isinstance(args[2], dict):
                        payload_options = args[2]
                else:
                    raise TypeError("llm.chat expects either (prompt) or (system, user, options)")

            requested_tokens = payload_options.get("max_tokens")
            requested_temperature = payload_options.get("temperature")
            max_t = requested_tokens if isinstance(requested_tokens, int) and 16 <= requested_tokens <= 4096 else max_tokens_int
            temp = (
                float(requested_temperature)
                if isinstance(requested_temperature, (int, float)) and 0 <= float(requested_temperature) <= 1
                else base_temperature_float
            )

            async def _run() -> dict:
                from app.llm import llm_provider

                text = await llm_provider.chat(
                    messages=[
                        {"role": "system", "content": str(system or "")[:8000]},
                        {"role": "user", "content": str(user or "")[:12000]},
                    ],
                    model=selected_model,
                    temperature=temp,
                    max_tokens=max_t,
                )
                return {"text": str(text or "")}

            out = asyncio.run(_run())
            text = str(out.get("text") or "")
            return text if simple_prompt_mode else {"text": text}

        context["llm"] = {"chat": llm_chat}
        return context

    @staticmethod
    def _safe_import(name: str, globals_: dict | None = None, locals_: dict | None = None, fromlist=(), level: int = 0):
        del globals_, locals_, fromlist, level
        root = str(name or "").split(".")[0]
        if root not in _DYNAMIC_SKILL_ALLOWED_IMPORTS:
            raise ImportError(f"import '{root}' is not allowed in Dynamic Skill")
        return __import__(name)

    @classmethod
    def _execute_python_skill_sync(
        cls,
        skill_code: str,
        function_name: str,
        params: dict,
        context: dict,
    ) -> Any:
        safe_builtins = {
            "str": str,
            "int": int,
            "float": float,
            "bool": bool,
            "type": type,
            "dict": dict,
            "list": list,
            "tuple": tuple,
            "set": set,
            "len": len,
            "min": min,
            "max": max,
            "sum": sum,
            "abs": abs,
            "range": range,
            "enumerate": enumerate,
            "zip": zip,
            "sorted": sorted,
            "any": any,
            "all": all,
            "isinstance": isinstance,
            "callable": callable,
            "print": print,
            "__import__": cls._safe_import,
        }
        runtime_globals: dict[str, Any] = {"__builtins__": safe_builtins}
        exec(compile(skill_code, "skill.py", "exec"), runtime_globals, runtime_globals)  # noqa: S102

        fn = runtime_globals.get(function_name)
        if not callable(fn):
            raise RuntimeError(f"Skill function '{function_name}' is not callable")

        return fn(params, context)


dynamic_tool_service = DynamicToolService()
