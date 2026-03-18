from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.dynamic_tool import DynamicTool
from app.services.api_executor import api_executor, resolve_url_template
from app.services.auth_data_security_service import auth_data_security_service
from app.services.dynamic_skill_runner_client import dynamic_skill_runner_client

logger = logging.getLogger(__name__)
_SKILL_RUNNER_FAILED = "Skill runner failed"


def _normalize_tool_name(name: str) -> str:
    return str(name or "").removeprefix("dyn:").removeprefix("dyn_").strip().lower().replace("-", "_").replace(" ", "_")


def _read_skill_file(path: Path) -> str:
    if path.exists() and path.is_file():
        return path.read_text(encoding="utf-8")
    return ""


def _load_skill_code(tool: DynamicTool) -> tuple[str, str | None]:
    auth_data = tool.auth_data if isinstance(tool.auth_data, dict) else {}
    headers = tool.headers if isinstance(tool.headers, dict) else {}
    skill_path = str(auth_data.get("skill_path") or "").strip()
    skill_code_inline = str(auth_data.get("skill_code_inline") or "").strip()

    skill_code = ""
    if skill_path:
        skill_code = _read_skill_file(Path(skill_path))

    if not skill_code:
        storage_dir = str(auth_data.get("storage_dir") or "").strip()
        if storage_dir:
            skill_code = _read_skill_file(Path(storage_dir) / "skill.py")

    if not skill_code and skill_code_inline:
        skill_code = skill_code_inline

    function_name = str(headers.get("function") or "run").strip() or "run"
    return skill_code, function_name


async def call_dynamic_tool(
    service: object,
    *,
    db: AsyncSession,
    user_id: UUID,
    tool_name: str,
    arguments: dict,
) -> dict:
    clean_name = _normalize_tool_name(tool_name)
    tool = await service._get_by_name(db, user_id, clean_name)
    if not tool:
        return {"success": False, "error": f"Dynamic tool '{clean_name}' not found"}
    if not tool.is_active:
        return {"success": False, "error": f"Dynamic tool '{clean_name}' is disabled"}

    method = (tool.method or "GET").upper()
    endpoint = str(tool.endpoint or "").strip()
    if method == "PYTHON" or endpoint.startswith("python://"):
        return await call_python_skill(service, user_id=user_id, tool=tool, arguments=arguments)

    headers = _resolve_runtime_headers(tool)
    url = resolve_url_template(tool.endpoint, arguments if tool.method == "GET" else {})
    body = arguments if method in ("POST", "PUT", "PATCH") else None
    try:
        async with asyncio.timeout(30):
            result = await api_executor.call(method=method, url=url, headers=headers, body=body)
        return {
            "success": result.get("status_code", 0) < 400,
            "status_code": result.get("status_code"),
            "data": _parse_response_body(result.get("body", "")),
            "tool_name": clean_name,
            "endpoint": tool.endpoint,
        }
    except asyncio.TimeoutError:
        return {"success": False, "error": f"Timeout calling {tool.endpoint}"}
    except Exception as exc:
        logger.warning("dynamic tool call failed: %s — %s", clean_name, exc)
        return {"success": False, "error": str(exc)}


def _resolve_runtime_headers(tool: DynamicTool) -> dict:
    headers = dict(tool.headers) if tool.headers else {}
    auth_data_raw = tool.auth_data or {}
    if not auth_data_raw:
        return headers
    auth_data, _ = auth_data_security_service.resolve_for_runtime(auth_data_raw)
    if token := auth_data.get("token"):
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _parse_response_body(body_text: Any) -> Any:
    try:
        return json.loads(body_text)
    except (json.JSONDecodeError, TypeError):
        return body_text


def _resolve_timeout_seconds(capabilities: dict) -> int:
    timeout_seconds = 30
    llm_caps = capabilities.get("llm") if isinstance(capabilities, dict) else None
    if isinstance(llm_caps, dict):
        timeout_candidate = llm_caps.get("timeout_seconds")
        if isinstance(timeout_candidate, int) and 1 <= timeout_candidate <= 60:
            timeout_seconds = timeout_candidate
    return timeout_seconds


async def _record_skill_finish(
    service: object,
    *,
    user_id: UUID,
    tool_name: str,
    execution_mode: str,
    execution_id: str,
    success: bool,
    error: str | None = None,
) -> None:
    service._log_skill_execution_event(
        event="execute_finish",
        user_id=user_id,
        tool_name=tool_name,
        execution_mode=execution_mode,
        success=success,
        error=error,
    )
    await service._persist_skill_execution_event(
        event="execute_finish",
        user_id=user_id,
        tool_name=tool_name,
        execution_mode=execution_mode,
        execution_id=execution_id,
        success=success,
        error=error or "",
    )


async def call_python_skill(service: object, *, user_id: UUID, tool: DynamicTool, arguments: dict) -> dict:
    headers = tool.headers if isinstance(tool.headers, dict) else {}
    try:
        skill_code, function_name = _load_skill_code(tool)
    except Exception as exc:
        return {"success": False, "error": f"Failed to read skill code: {exc}"}

    if not skill_code:
        auth_data = tool.auth_data if isinstance(tool.auth_data, dict) else {}
        skill_path = str(auth_data.get("skill_path") or "").strip()
        if not skill_path:
            return {
                "success": False,
                "error": "Dynamic Python Skill is not installed correctly (missing skill_path and inline backup code).",
            }
        return {
            "success": False,
            "error": "Dynamic Python Skill code file is missing. Re-upload the skill package to restore local runtime files.",
        }

    capabilities = headers.get("capabilities") if isinstance(headers.get("capabilities"), dict) else {}
    execution_id = uuid4().hex
    service._log_skill_execution_event(
        event="execute_start",
        user_id=user_id,
        tool_name=str(tool.name),
        execution_mode="runner",
    )
    await service._persist_skill_execution_event(
        event="execute_start",
        user_id=user_id,
        tool_name=str(tool.name),
        execution_mode="runner",
        execution_id=execution_id,
        payload={"function_name": function_name},
    )

    try:
        service._validate_python_skill_code(skill_code=skill_code, function_name=function_name)
    except ValueError as exc:
        return {"success": False, "error": f"Skill validation failed: {exc}"}

    runtime_context = service._build_python_skill_context(
        user_id=user_id,
        tool_name=str(tool.name),
        capabilities=capabilities,
    )
    timeout_seconds = _resolve_timeout_seconds(capabilities)

    del runtime_context
    return await _execute_runner_mode(
        service,
        user_id=user_id,
        tool_name=str(tool.name),
        skill_code=skill_code,
        function_name=function_name,
        arguments=arguments,
        capabilities=capabilities,
        timeout_seconds=timeout_seconds,
        execution_id=execution_id,
    )


async def _execute_runner_mode(
    service: object,
    *,
    user_id: UUID,
    tool_name: str,
    skill_code: str,
    function_name: str,
    arguments: dict,
    capabilities: dict,
    timeout_seconds: int,
    execution_id: str,
) -> dict:
    try:
        serializable_context = service._build_runner_context(
            user_id=user_id,
            tool_name=tool_name,
            capabilities=capabilities,
            execution_id=execution_id,
        )
        runner_result = await dynamic_skill_runner_client.execute_skill(
            skill_code=skill_code,
            function_name=function_name,
            params=dict(arguments or {}),
            context=serializable_context,
            timeout_seconds=timeout_seconds,
        )
        success = bool(runner_result.get("success"))
        if not success:
            error = str(runner_result.get("error") or _SKILL_RUNNER_FAILED)
            await _record_skill_finish(
                service,
                user_id=user_id,
                tool_name=tool_name,
                execution_mode="runner",
                execution_id=execution_id,
                success=False,
                error=error,
            )
            return {"success": False, "error": error}
        data = runner_result.get("result") if isinstance(runner_result.get("result"), dict) else {"result": runner_result.get("result")}
        await _record_skill_finish(
            service,
            user_id=user_id,
            tool_name=tool_name,
            execution_mode="runner",
            execution_id=execution_id,
            success=True,
        )
        return {"success": True, "tool_name": tool_name, "data": data}
    except Exception as exc:
        logger.warning("dynamic python skill runner mode failed: %s — %s", tool_name, exc)
        await _record_skill_finish(
            service,
            user_id=user_id,
            tool_name=tool_name,
            execution_mode="runner",
            execution_id=execution_id,
            success=False,
            error=str(exc),
        )
        return {"success": False, "error": str(exc)}
