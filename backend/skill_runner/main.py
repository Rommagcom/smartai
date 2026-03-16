from __future__ import annotations

import asyncio
import base64
import hmac
import importlib
import json
import logging
import os
from time import perf_counter
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.llm import llm_provider
from app.services.dynamic_skill_audit_service import dynamic_skill_audit_service
from app.services.egress_policy_service import egress_policy_service
from app.services.http_client_service import http_client_service

app = FastAPI(title="SmartAI Skill Runner", version="1.0.0")
logger = logging.getLogger(__name__)


class ExecuteSkillRequest(BaseModel):
    skill_code: str = Field(min_length=1, max_length=2_000_000)
    function_name: str = Field(default="run", min_length=1, max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=30, ge=1, le=120)


class ExecuteSkillResponse(BaseModel):
    success: bool
    result: dict[str, Any] | Any | None = None
    error: str | None = None


class LlmCallbackRequest(BaseModel):
    system: str = ""
    user: str = ""
    options: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    user_id: str = ""
    tool_name: str = ""
    execution_id: str = ""


class LlmCallbackResponse(BaseModel):
    text: str


class HttpCallbackRequest(BaseModel):
    method: str = Field(default="GET", min_length=3, max_length=16)
    url: str = Field(min_length=1, max_length=4000)
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    body: Any = None
    user_id: str = ""
    tool_name: str = ""
    execution_id: str = ""


class HttpCallbackResponse(BaseModel):
    status_code: int
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None


def _select_llm_model(llm_caps: dict[str, Any]) -> str | None:
    allowed_models = llm_caps.get("allowed_models") if isinstance(llm_caps, dict) else []
    if not isinstance(allowed_models, list) or not allowed_models:
        return None
    candidate = str(allowed_models[0] or "").strip()
    return candidate if candidate and candidate != "default" else None


def _resolve_llm_limits(payload_options: dict[str, Any], llm_caps: dict[str, Any]) -> tuple[int, float]:
    capability_max_tokens = llm_caps.get("max_tokens") if isinstance(llm_caps, dict) else None
    capability_temperature = llm_caps.get("temperature") if isinstance(llm_caps, dict) else None
    max_tokens = payload_options.get("max_tokens")
    temperature = payload_options.get("temperature")

    if isinstance(max_tokens, int) and 16 <= max_tokens <= 4096:
        capped_max_tokens = max_tokens
    elif isinstance(capability_max_tokens, int) and 32 <= capability_max_tokens <= 4096:
        capped_max_tokens = capability_max_tokens
    else:
        capped_max_tokens = 1024

    if isinstance(temperature, (int, float)) and 0 <= float(temperature) <= 1:
        capped_temperature = float(temperature)
    elif isinstance(capability_temperature, (int, float)) and 0 <= float(capability_temperature) <= 1:
        capped_temperature = float(capability_temperature)
    else:
        capped_temperature = 0.2

    return capped_max_tokens, capped_temperature


def _runner_secret() -> str:
    return str(os.getenv("DYNAMIC_SKILL_RUNNER_SECRET", "")).strip()


def _audit_event(event: str, **context: Any) -> None:
    logger.info(
        "skill runner event",
        extra={
            "context": {
                "component": "skill_runner",
                "event": event,
                **context,
            }
        },
    )


def _sandbox_image() -> str:
    return str(os.getenv("DYNAMIC_SKILL_SANDBOX_IMAGE", "smartai-skill-runner:latest")).strip()


def _sandbox_memory_limit() -> str:
    return str(os.getenv("SANDBOX_MEMORY_LIMIT", "256m")).strip() or "256m"


def _sandbox_cpu_limit() -> float:
    raw = str(os.getenv("SANDBOX_CPU_LIMIT", "0.5")).strip()
    try:
        return max(0.1, min(4.0, float(raw)))
    except ValueError:
        return 0.5


def _sandbox_egress_enabled() -> bool:
    return str(os.getenv("SANDBOX_EGRESS_ENABLED", "false")).strip().lower() in {"1", "true", "yes", "on"}


def _llm_callback_enabled() -> bool:
    return str(os.getenv("DYNAMIC_SKILL_LLM_CALLBACK_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}


def _http_callback_enabled() -> bool:
    return str(os.getenv("DYNAMIC_SKILL_HTTP_CALLBACK_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}


def _sandbox_timeout(default_timeout: int) -> int:
    raw = str(os.getenv("DYNAMIC_SKILL_SANDBOX_TIMEOUT_SECONDS", str(default_timeout))).strip()
    try:
        return max(1, min(120, int(raw)))
    except ValueError:
        return default_timeout


def _sandbox_network() -> str:
    return str(os.getenv("DYNAMIC_SKILL_SANDBOX_NETWORK", "smartai-sandbox-net")).strip() or "smartai-sandbox-net"


def _callback_url() -> str:
    return str(os.getenv("DYNAMIC_SKILL_RUNNER_CALLBACK_URL", "http://skill-runner:8081/callback/llm")).strip()


def _http_callback_url() -> str:
    return str(os.getenv("DYNAMIC_SKILL_RUNNER_HTTP_CALLBACK_URL", "http://skill-runner:8081/callback/http")).strip()


def _http_timeout_seconds() -> int:
    raw = str(os.getenv("DYNAMIC_SKILL_HTTP_TIMEOUT_SECONDS", "30")).strip()
    try:
        return max(1, min(120, int(raw)))
    except ValueError:
        return 30


def _resolve_network_mode(*, llm_enabled: bool, http_enabled: bool) -> str:
    if llm_enabled or http_enabled:
        return _sandbox_network()
    if _sandbox_egress_enabled():
        return "bridge"
    return "none"


def _skill_context_value(payload: ExecuteSkillRequest, key: str) -> str:
    context = payload.context if isinstance(payload.context, dict) else {}
    return str(context.get(key) or "").strip()


def _http_method(method: str) -> str:
    normalized = str(method or "GET").strip().upper()
    return normalized if normalized in {"GET", "POST", "PUT", "PATCH", "DELETE"} else "GET"


def _sanitize_http_headers(headers: dict[str, str]) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    for key, value in headers.items():
        header_key = str(key or "").strip()
        if not header_key:
            continue
        sanitized[header_key] = str(value or "")
    return sanitized


async def _persist_audit_event(
    *,
    event_type: str,
    source: str,
    execution_mode: str,
    tool_name: str,
    execution_id: str,
    user_id: str = "",
    success: bool | None = None,
    error_text: str = "",
    payload: dict[str, Any] | None = None,
) -> None:
    parsed_user_id = None
    if user_id:
        try:
            from uuid import UUID

            parsed_user_id = UUID(user_id)
        except ValueError:
            parsed_user_id = None

    try:
        await dynamic_skill_audit_service.record_event(
            event_type=event_type,
            source=source,
            execution_mode=execution_mode,
            tool_name=tool_name,
            execution_id=execution_id,
            user_id=parsed_user_id,
            success=success,
            error_text=error_text,
            payload=payload,
        )
    except Exception as exc:
        logger.warning("skill runner audit persistence failed: %s", exc)


def _ensure_runner_auth(header_secret: str | None) -> None:
    expected = _runner_secret()
    if not expected:
        return
    if not header_secret or not hmac.compare_digest(str(header_secret).strip(), expected):
        raise HTTPException(status_code=401, detail="Invalid runner secret")


def _run_skill_sync(payload: ExecuteSkillRequest) -> dict[str, Any]:
    docker_module = importlib.import_module("docker")
    client = docker_module.from_env()
    container = None
    request_blob = {
        "skill_code": payload.skill_code,
        "function_name": payload.function_name,
        "params": payload.params,
        "context": payload.context,
    }
    request_b64 = base64.b64encode(json.dumps(request_blob, ensure_ascii=False).encode("utf-8")).decode("ascii")

    llm_enabled = _llm_callback_enabled() and bool((payload.context or {}).get("capabilities", {}).get("llm", {}).get("enabled"))
    http_enabled = _http_callback_enabled()
    network_mode = _resolve_network_mode(llm_enabled=llm_enabled, http_enabled=http_enabled)
    nano_cpus = int(_sandbox_cpu_limit() * 1_000_000_000)
    started_at = perf_counter()
    tool_name = _skill_context_value(payload, "tool_name")
    user_id = _skill_context_value(payload, "user_id")

    _audit_event(
        "execute_start",
        tool_name=tool_name,
        user_id=user_id,
        timeout_seconds=payload.timeout_seconds,
        network_mode=network_mode,
        llm_enabled=llm_enabled,
        http_enabled=http_enabled,
    )

    try:
        container = client.containers.run(
            image=_sandbox_image(),
            command=["python", "/app/skill_runner/sandbox_entrypoint.py"],
            detach=True,
            auto_remove=False,
            environment={
                "SKILL_REQUEST_B64": request_b64,
                "SKILL_RUNNER_CALLBACK_URL": _callback_url(),
                "SKILL_RUNNER_HTTP_CALLBACK_URL": _http_callback_url(),
                "SKILL_RUNNER_SECRET": _runner_secret(),
            },
            network_mode=network_mode,
            mem_limit=_sandbox_memory_limit(),
            nano_cpus=nano_cpus,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            pids_limit=64,
            user="10001:10001",
        )

        effective_timeout = _sandbox_timeout(payload.timeout_seconds)
        wait_result = container.wait(timeout=effective_timeout)
        status_code = int((wait_result or {}).get("StatusCode") or 1)
        stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="ignore").strip()
        stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="ignore").strip()

        if status_code != 0:
            _audit_event(
                "execute_finish",
                tool_name=tool_name,
                user_id=user_id,
                success=False,
                status_code=status_code,
                duration_ms=round((perf_counter() - started_at) * 1000, 2),
            )
            return {"success": False, "error": (stderr or stdout or "sandbox execution failed")[:2000]}

        try:
            result = json.loads(stdout) if stdout else {"result": None}
        except json.JSONDecodeError:
            return {"success": False, "error": f"sandbox returned invalid JSON: {stdout[:500]}"}

        if not isinstance(result, dict):
            _audit_event(
                "execute_finish",
                tool_name=tool_name,
                user_id=user_id,
                success=False,
                status_code=status_code,
                duration_ms=round((perf_counter() - started_at) * 1000, 2),
                error="sandbox returned non-object result",
            )
            return {"success": False, "error": "sandbox returned non-object result"}
        _audit_event(
            "execute_finish",
            tool_name=tool_name,
            user_id=user_id,
            success=bool(result.get("success", True)),
            status_code=status_code,
            duration_ms=round((perf_counter() - started_at) * 1000, 2),
        )
        return {
            "success": bool(result.get("success", True)),
            "result": result.get("result"),
            "error": result.get("error"),
        }
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception as exc:
                logger.debug("failed to remove sandbox container: %s", exc)


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/callback/llm",
    responses={
        401: {"description": "Invalid runner secret"},
        500: {"description": "LLM callback failed"},
    },
)
async def llm_callback(
    payload: LlmCallbackRequest,
    x_runner_secret: Annotated[str | None, Header(alias="X-Runner-Secret")] = None,
) -> LlmCallbackResponse:
    _ensure_runner_auth(x_runner_secret)

    llm_caps = payload.capabilities.get("llm") if isinstance(payload.capabilities, dict) else {}
    requested_options = payload.options if isinstance(payload.options, dict) else {}
    selected_model = _select_llm_model(llm_caps if isinstance(llm_caps, dict) else {})
    capped_max_tokens, capped_temperature = _resolve_llm_limits(
        requested_options,
        llm_caps if isinstance(llm_caps, dict) else {},
    )

    try:
        started_at = perf_counter()
        text = await llm_provider.chat(
            messages=[
                {"role": "system", "content": str(payload.system or "")[:8000]},
                {"role": "user", "content": str(payload.user or "")[:12000]},
            ],
            model=selected_model,
            temperature=capped_temperature,
            max_tokens=capped_max_tokens,
        )
        _audit_event(
            "llm_callback",
            tool_name=payload.tool_name,
            execution_id=payload.execution_id,
            model=selected_model or "default",
            max_tokens=capped_max_tokens,
            duration_ms=round((perf_counter() - started_at) * 1000, 2),
        )
        await _persist_audit_event(
            event_type="llm_callback",
            source="skill_runner",
            execution_mode="runner",
            tool_name=payload.tool_name,
            execution_id=payload.execution_id,
            user_id=payload.user_id,
            success=True,
            payload={"model": selected_model or "default", "max_tokens": capped_max_tokens},
        )
    except Exception as exc:
        await _persist_audit_event(
            event_type="llm_callback",
            source="skill_runner",
            execution_mode="runner",
            tool_name=payload.tool_name,
            execution_id=payload.execution_id,
            user_id=payload.user_id,
            success=False,
            error_text=str(exc),
        )
        raise HTTPException(status_code=500, detail=f"llm callback failed: {exc}") from exc

    return LlmCallbackResponse(text=str(text or ""))


@app.post(
    "/callback/http",
    responses={
        401: {"description": "Invalid runner secret"},
        500: {"description": "HTTP callback failed"},
    },
)
async def http_callback(
    payload: HttpCallbackRequest,
    x_runner_secret: Annotated[str | None, Header(alias="X-Runner-Secret")] = None,
) -> HttpCallbackResponse:
    _ensure_runner_auth(x_runner_secret)

    if not _http_callback_enabled():
        raise HTTPException(status_code=500, detail="HTTP callback is disabled")

    method = _http_method(payload.method)
    try:
        validated_url = egress_policy_service.validate_url(payload.url)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"HTTP callback blocked: {exc}") from exc

    client = http_client_service.get()
    started_at = perf_counter()
    try:
        response = await client.request(
            method=method,
            url=validated_url,
            headers=_sanitize_http_headers(payload.headers),
            params=payload.params if isinstance(payload.params, dict) else None,
            json=payload.body,
            timeout=_http_timeout_seconds(),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"HTTP callback failed: {exc}") from exc

    parsed_body: Any
    try:
        parsed_body = response.json()
    except Exception:
        parsed_body = response.text

    _audit_event(
        "http_callback",
        tool_name=payload.tool_name,
        execution_id=payload.execution_id,
        method=method,
        url=validated_url,
        status_code=response.status_code,
        duration_ms=round((perf_counter() - started_at) * 1000, 2),
    )
    await _persist_audit_event(
        event_type="http_callback",
        source="skill_runner",
        execution_mode="runner",
        tool_name=payload.tool_name,
        execution_id=payload.execution_id,
        user_id=payload.user_id,
        success=response.status_code < 400,
        payload={"method": method, "url": validated_url, "status_code": response.status_code},
    )
    return HttpCallbackResponse(
        status_code=response.status_code,
        headers=dict(response.headers),
        body=parsed_body,
    )


@app.post(
    "/execute",
    responses={
        401: {"description": "Invalid runner secret"},
        500: {"description": "Runner execution failed"},
    },
)
async def execute_skill(
    payload: ExecuteSkillRequest,
    x_runner_secret: Annotated[str | None, Header(alias="X-Runner-Secret")] = None,
) -> ExecuteSkillResponse:
    _ensure_runner_auth(x_runner_secret)

    tool_name = _skill_context_value(payload, "tool_name")
    user_id = _skill_context_value(payload, "user_id")
    execution_id = _skill_context_value(payload, "execution_id")
    await _persist_audit_event(
        event_type="execute_start",
        source="skill_runner",
        execution_mode="runner",
        tool_name=tool_name,
        execution_id=execution_id,
        user_id=user_id,
        payload={"function_name": payload.function_name},
    )

    try:
        result = await asyncio.to_thread(_run_skill_sync, payload)
    except Exception as exc:
        await _persist_audit_event(
            event_type="execute_finish",
            source="skill_runner",
            execution_mode="runner",
            tool_name=tool_name,
            execution_id=execution_id,
            user_id=user_id,
            success=False,
            error_text=str(exc),
        )
        raise HTTPException(status_code=500, detail=f"runner failed: {exc}") from exc

    await _persist_audit_event(
        event_type="execute_finish",
        source="skill_runner",
        execution_mode="runner",
        tool_name=tool_name,
        execution_id=execution_id,
        user_id=user_id,
        success=bool(result.get("success")),
        error_text=str(result.get("error") or ""),
    )

    return ExecuteSkillResponse(
        success=bool(result.get("success")),
        result=result.get("result"),
        error=result.get("error"),
    )
