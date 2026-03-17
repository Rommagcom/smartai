from __future__ import annotations

import ast
import asyncio
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from uuid import UUID

SKILL_MANIFEST_FILE = "manifest.json"
SKILL_CODE_FILE = "skill.py"
SKILL_README_FILE = "skill.md"


def validate_python_skill_code(*, skill_code: str, function_name: str) -> None:
    try:
        module_ast = ast.parse(skill_code, filename=SKILL_CODE_FILE)
    except SyntaxError as exc:
        raise ValueError(f"syntax error: {exc}") from exc

    import_roots = _collect_import_roots(module_ast)
    _validate_import_roots(import_roots)
    if not _has_function(module_ast, function_name):
        raise ValueError(f"function '{function_name}' not found in {SKILL_CODE_FILE}")


def _collect_import_roots(module_ast: ast.AST) -> list[str]:
    roots: list[str] = []
    for node in ast.walk(module_ast):
        if isinstance(node, ast.Import):
            roots.extend(
                str(alias.name or "").split(".")[0]
                for alias in node.names
                if str(alias.name or "").strip()
            )
        elif isinstance(node, ast.ImportFrom) and str(node.module or "").strip():
            roots.append(str(node.module or "").split(".")[0])
    return [root for root in roots if root]


def _validate_import_roots(import_roots: list[str]) -> None:
    del import_roots


def _has_function(module_ast: ast.AST, function_name: str) -> bool:
    return any(
        isinstance(node, ast.FunctionDef) and node.name == function_name
        for node in ast.walk(module_ast)
    )


def extract_description_from_md(skill_md: str) -> str:
    lines = [line.strip() for line in str(skill_md or "").splitlines() if line.strip()]
    if not lines:
        return "Dynamic Python Skill"
    head = lines[0].lstrip("# ").strip()
    return head[:240] if head else "Dynamic Python Skill"


def dynamic_skill_storage_root() -> Path:
    backend_root = Path(__file__).resolve().parents[2]
    root = backend_root / "data" / "dynamic_skills"
    root.mkdir(parents=True, exist_ok=True)
    return root


def store_skill_package(
    *,
    user_id: UUID,
    name: str,
    version: str,
    skill_code: str,
    skill_md: str,
    manifest: dict,
    zip_payload: bytes,
) -> dict[str, str]:
    storage_root = dynamic_skill_storage_root()
    safe_version = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(version or "1.0.0"))
    skill_dir = storage_root / str(user_id) / name / safe_version
    skill_dir.mkdir(parents=True, exist_ok=True)

    skill_path = skill_dir / SKILL_CODE_FILE
    readme_path = skill_dir / SKILL_README_FILE
    manifest_path = skill_dir / SKILL_MANIFEST_FILE
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


def build_python_skill_context(*, user_id: UUID, tool_name: str, capabilities: dict) -> dict:
    context: dict[str, Any] = {
        "user_id": str(user_id),
        "tool_name": tool_name,
        "capabilities": capabilities,
    }

    llm_caps = capabilities.get("llm") if isinstance(capabilities, dict) else None
    if not (isinstance(llm_caps, dict) and bool(llm_caps.get("enabled"))):
        return context

    llm_config = _resolve_llm_config(llm_caps)

    def llm_chat(*args: Any, **kwargs: Any) -> dict | str:
        system, user, payload_options, simple_prompt_mode = _parse_llm_chat_args(args, kwargs)
        max_tokens = _resolve_llm_max_tokens(payload_options, llm_config["max_tokens"])
        temperature = _resolve_llm_temperature(payload_options, llm_config["temperature"])

        async def _run() -> dict:
            from app.llm import llm_provider

            text = await llm_provider.chat(
                messages=[
                    {"role": "system", "content": str(system or "")[:8000]},
                    {"role": "user", "content": str(user or "")[:12000]},
                ],
                model=llm_config["model"],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return {"text": str(text or "")}

        out = asyncio.run(_run())
        text = str(out.get("text") or "")
        return text if simple_prompt_mode else {"text": text}

    context["llm"] = {"chat": llm_chat}
    return context


def _resolve_llm_config(llm_caps: dict) -> dict[str, Any]:
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
    return {
        "max_tokens": max_tokens_int,
        "temperature": base_temperature_float,
        "model": selected_model,
    }


def _parse_llm_chat_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[str, str, dict, bool]:
    payload_options = kwargs.get("options") if isinstance(kwargs.get("options"), dict) else {}
    if "system" in kwargs or "user" in kwargs:
        return _parse_llm_chat_kwargs(args, kwargs, payload_options)
    return _parse_llm_chat_positional(args, payload_options)


def _parse_llm_chat_kwargs(args: tuple[Any, ...], kwargs: dict[str, Any], payload_options: dict) -> tuple[str, str, dict, bool]:
    system = str(kwargs.get("system") or "")
    user = str(kwargs.get("user") or "")
    if not user and args:
        user = str(args[0] or "")
    return system, user, payload_options, False


def _parse_llm_chat_positional(args: tuple[Any, ...], payload_options: dict) -> tuple[str, str, dict, bool]:
    if len(args) == 1:
        return "", str(args[0] or ""), payload_options, True
    if len(args) >= 2:
        if len(args) >= 3 and isinstance(args[2], dict):
            payload_options = args[2]
        return str(args[0] or ""), str(args[1] or ""), payload_options, False
    raise TypeError("llm.chat expects either (prompt) or (system, user, options)")


def _resolve_llm_max_tokens(payload_options: dict, default: int) -> int:
    requested_tokens = payload_options.get("max_tokens")
    if isinstance(requested_tokens, int) and 16 <= requested_tokens <= 4096:
        return requested_tokens
    return default


def _resolve_llm_temperature(payload_options: dict, default: float) -> float:
    requested_temperature = payload_options.get("temperature")
    if isinstance(requested_temperature, (int, float)) and 0 <= float(requested_temperature) <= 1:
        return float(requested_temperature)
    return default


def build_runner_context(*, user_id: UUID, tool_name: str, capabilities: dict, execution_id: str) -> dict[str, Any]:
    return {
        "user_id": str(user_id),
        "tool_name": tool_name,
        "capabilities": capabilities if isinstance(capabilities, dict) else {},
        "execution_id": execution_id,
    }
