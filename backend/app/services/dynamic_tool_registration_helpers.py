from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dynamic_tool import DynamicTool
from app.models.user import User
from app.services.auth_data_security_service import auth_data_security_service
from app.services.egress_policy_service import egress_policy_service

logger = logging.getLogger(__name__)


def _normalize_name(raw_name: str) -> str:
    return raw_name.strip().lower().replace("-", "_").replace(" ", "_")


def _build_auth_data(auth_token: str | None) -> dict:
    if not auth_token:
        return {}
    return auth_data_security_service.encrypt({"token": auth_token})


async def _upsert_dynamic_api_tool(
    service: object,
    *,
    db: AsyncSession,
    user_id: UUID,
    name: str,
    payload: object,
    auth_data: dict,
) -> tuple[DynamicTool, str]:
    existing = await service._get_by_name(db, user_id, name)
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
        return existing, "updated"

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
    return tool, "registered"


async def _sync_api_tool_vector(*, user_id: UUID, name: str, payload: object) -> None:
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


def _validate_skill_upload(filename: str, content: bytes, *, max_zip_bytes: int) -> str | None:
    safe_filename = str(filename or "add_skill.zip").strip() or "add_skill.zip"
    if not safe_filename.lower().endswith(".zip"):
        return "Skill package must be a .zip archive."
    if not isinstance(content, (bytes, bytearray)) or not content:
        return "Skill package is empty."
    if len(content) > max_zip_bytes:
        return f"Skill package is too large ({len(content)} bytes). Max: {max_zip_bytes} bytes."
    return None


def _parse_skill_package_or_error(service: object, content: bytes) -> tuple[dict | None, dict | None]:
    try:
        return service._parse_skill_zip(content), None
    except ValueError as exc:
        return None, {"status": "failed", "message": f"Invalid skill package: {exc}"}


def _extract_skill_manifest_data(service: object, parsed: dict) -> tuple[dict, dict | None]:
    manifest = parsed["manifest"]
    skill_code = parsed["skill_py"]
    skill_md = parsed["skill_md"]
    name = str(manifest.get("name") or "").strip().lower()
    entrypoint = str(manifest.get("entrypoint") or "skill.py").strip() or "skill.py"
    function_name = str(manifest.get("function") or "run").strip() or "run"
    description = str(manifest.get("description") or "").strip() or service._extract_description_from_md(skill_md)
    parameters_schema = manifest.get("input_schema") if isinstance(manifest.get("input_schema"), dict) else {}
    capabilities = manifest.get("capabilities") if isinstance(manifest.get("capabilities"), dict) else {}
    return {
        "manifest": manifest,
        "skill_code": skill_code,
        "skill_md": skill_md,
        "name": name,
        "entrypoint": entrypoint,
        "function_name": function_name,
        "description": description,
        "parameters_schema": parameters_schema,
        "capabilities": capabilities,
    }, None


def _validate_skill_name(name: str, safe_name_pattern: object) -> dict | None:
    if safe_name_pattern.match(name):
        return None
    return {
        "status": "failed",
        "message": f"Invalid skill name '{name}'. Use latin snake_case, 2-63 chars.",
    }


def _validate_skill_code_or_error(service: object, *, skill_code: str, function_name: str) -> dict | None:
    try:
        service._validate_python_skill_code(skill_code=skill_code, function_name=function_name)
        return None
    except ValueError as exc:
        return {"status": "failed", "message": f"Skill code validation failed: {exc}"}


def _build_python_skill_runtime_payload(
    *,
    storage_meta: dict,
    skill_code: str,
    entrypoint: str,
    function_name: str,
    capabilities: dict,
    manifest: dict,
) -> tuple[str, str, dict, dict]:
    method = "PYTHON"
    endpoint = f"python://{storage_meta['storage_dir'].split('/')[-2] if '/' in storage_meta['storage_dir'] else storage_meta['storage_dir']}"
    # endpoint is overridden by caller with normalized name to avoid path-coupling
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
        "skill_code_inline": skill_code,
    }
    return method, endpoint, headers, auth_data


async def _upsert_python_skill_tool(
    service: object,
    *,
    db: AsyncSession,
    user_id: UUID,
    name: str,
    description: str,
    endpoint: str,
    method: str,
    headers: dict,
    auth_data: dict,
    parameters_schema: dict,
    response_hint: str,
) -> tuple[DynamicTool, str]:
    existing = await service._get_by_name(db, user_id, name)
    if existing:
        existing.description = description
        existing.endpoint = endpoint
        existing.method = method
        existing.headers = headers
        existing.auth_data = auth_data
        existing.parameters_schema = parameters_schema
        existing.response_hint = response_hint
        existing.is_active = True
        db.add(existing)
        await db.commit()
        await db.refresh(existing)
        return existing, "updated"

    tool = DynamicTool(
        user_id=user_id,
        name=name,
        description=description,
        endpoint=endpoint,
        method=method,
        headers=headers,
        auth_data=auth_data,
        parameters_schema=parameters_schema,
        response_hint=response_hint,
        is_active=True,
    )
    db.add(tool)
    await db.commit()
    await db.refresh(tool)
    return tool, "registered"


async def _sync_python_skill_vector(
    *,
    user_id: UUID,
    name: str,
    description: str,
    endpoint: str,
    method: str,
    parameters_schema: dict,
    capabilities: dict,
    manifest_version: str,
) -> None:
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
            metadata={"capabilities": capabilities, "manifest_version": manifest_version},
        )
    except Exception as exc:
        logger.debug("failed to sync dynamic python skill to Milvus: %s", exc)


async def register_from_user_message(
    service: object,
    *,
    db: AsyncSession,
    user_id: UUID,
    user_message: str,
    meta_registration_prompt: str,
    safe_name_pattern: object,
) -> dict:
    from app.llm import llm_provider
    from app.schemas.dynamic_tool import ApiRegistrationPayload

    payload = await llm_provider.chat_structured(
        response_model=ApiRegistrationPayload,
        messages=[
            {"role": "system", "content": meta_registration_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0.0,
    )

    if not payload:
        return {
            "status": "failed",
            "message": "Не удалось извлечь описание API из сообщения. Укажите URL, параметры и название.",
        }

    name = _normalize_name(payload.tool_name)
    if not safe_name_pattern.match(name):
        return {
            "status": "failed",
            "message": f"Некорректное имя инструмента: '{name}'. Используйте латиницу, snake_case, 2-63 символа.",
        }

    try:
        egress_policy_service.validate_url(payload.api_endpoint)
    except ValueError as exc:
        return {"status": "failed", "message": f"URL заблокирован политикой безопасности: {exc}"}

    auth_data = _build_auth_data(payload.auth_token)
    tool, status = await _upsert_dynamic_api_tool(
        service,
        db=db,
        user_id=user_id,
        name=name,
        payload=payload,
        auth_data=auth_data,
    )

    logger.info(
        "dynamic tool %s: %s (user=%s, endpoint=%s)",
        status,
        name,
        user_id,
        payload.api_endpoint,
    )

    await _sync_api_tool_vector(user_id=user_id, name=name, payload=payload)

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
    service: object,
    *,
    db: AsyncSession,
    user_id: UUID,
    filename: str,
    content: bytes,
    safe_name_pattern: object,
    max_zip_bytes: int,
) -> dict:
    user_row = (await db.execute(service._user_select(user_id))).scalar_one_or_none()
    if not user_row or not bool(getattr(user_row, "is_admin", False)):
        return {"status": "failed", "message": "Only administrators can upload Dynamic Skills"}

    validation_error = _validate_skill_upload(filename, content, max_zip_bytes=max_zip_bytes)
    if validation_error:
        return {"status": "failed", "message": validation_error}

    parsed, parse_error = _parse_skill_package_or_error(service, content)
    if parse_error is not None:
        return parse_error

    skill_data, _ = _extract_skill_manifest_data(service, parsed or {})
    name_error = _validate_skill_name(skill_data["name"], safe_name_pattern)
    if name_error is not None:
        return name_error

    code_error = _validate_skill_code_or_error(
        service,
        skill_code=skill_data["skill_code"],
        function_name=skill_data["function_name"],
    )
    if code_error is not None:
        return code_error

    storage_meta = service._store_skill_package(
        user_id=user_id,
        name=skill_data["name"],
        version=str(skill_data["manifest"].get("version") or "1.0.0"),
        skill_code=skill_data["skill_code"],
        skill_md=skill_data["skill_md"],
        manifest=skill_data["manifest"],
        zip_payload=bytes(content),
    )

    method, _unused_endpoint, headers, auth_data = _build_python_skill_runtime_payload(
        storage_meta=storage_meta,
        skill_code=skill_data["skill_code"],
        entrypoint=skill_data["entrypoint"],
        function_name=skill_data["function_name"],
        capabilities=skill_data["capabilities"],
        manifest=skill_data["manifest"],
    )
    endpoint = f"python://{skill_data['name']}"

    tool, status = await _upsert_python_skill_tool(
        service,
        db=db,
        user_id=user_id,
        name=skill_data["name"],
        description=skill_data["description"],
        endpoint=endpoint,
        method=method,
        headers=headers,
        auth_data=auth_data,
        parameters_schema=skill_data["parameters_schema"],
        response_hint=str(skill_data["manifest"].get("response_hint") or ""),
    )
    await _sync_python_skill_vector(
        user_id=user_id,
        name=skill_data["name"],
        description=skill_data["description"],
        endpoint=endpoint,
        method=method,
        parameters_schema=skill_data["parameters_schema"],
        capabilities=skill_data["capabilities"],
        manifest_version=str(skill_data["manifest"].get("version") or "1.0.0"),
    )

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
        "message": f"Dynamic Skill '{skill_data['name']}' {'updated' if status == 'updated' else 'registered'}.",
    }

