from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


BUILTIN_TOOL_NAMES = {"web_search", "web_fetch"}
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$")
_ARG_PRIMITIVE_TYPES = {"string", "integer", "number", "boolean", "object", "array"}


@dataclass(slots=True)
class DynamicTool:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    skill_dir: Path

    def to_ollama_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class DynamicToolRegistry:
    def __init__(self, skills_root: Path) -> None:
        self.skills_root = skills_root
        self._tools: dict[str, DynamicTool] = {}
        self._errors: dict[str, str] = {}
        self._allowlist = self._parse_tool_allowlist(os.getenv("DYNAMIC_TOOLS_ALLOWLIST", "*"))
        self._strict_args = self._read_bool("DYNAMIC_SKILL_STRICT_ARGS", default=True)
        self._require_integrity = self._read_bool("DYNAMIC_SKILL_REQUIRE_INTEGRITY", default=False)
        self._require_signature = self._read_bool("DYNAMIC_SKILL_REQUIRE_SIGNATURE", default=False)
        self._timeout_seconds = max(1, self._safe_int(os.getenv("DYNAMIC_SKILL_TIMEOUT_SECONDS"), default=20))
        self._max_string_length = max(
            32,
            self._safe_int(os.getenv("DYNAMIC_SKILL_MAX_STRING_LENGTH"), default=10000),
        )
        self._max_args_bytes = max(1024, self._safe_int(os.getenv("DYNAMIC_SKILL_MAX_ARGS_BYTES"), default=50000))

    @property
    def tools(self) -> dict[str, DynamicTool]:
        return self._tools

    @property
    def errors(self) -> dict[str, str]:
        return self._errors

    def refresh(self) -> dict[str, DynamicTool]:
        self._tools = {}
        self._errors = {}
        if not self.skills_root.exists():
            return self._tools

        for skill_dir in self.skills_root.iterdir():
            if not skill_dir.is_dir():
                continue

            manifest_path = skill_dir / "manifest.json"
            if not manifest_path.exists():
                continue
            self._load_skill_dir(skill_dir=skill_dir, manifest_path=manifest_path)

        return self._tools

    def _load_skill_dir(self, *, skill_dir: Path, manifest_path: Path) -> None:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            tool_name = str(manifest.get("name") or "").strip()
            if not tool_name:
                raise ValueError("Missing manifest field: name")
            if not self._is_tool_allowed(tool_name):
                self._errors[skill_dir.name] = f"Tool {tool_name} is blocked by DYNAMIC_TOOLS_ALLOWLIST"
                return

            tool = self._load_tool(skill_dir, manifest)
            duplicate_error = self._find_registry_conflict(tool.name)
            if duplicate_error:
                self._errors[skill_dir.name] = duplicate_error
                return

            self._tools[tool.name] = tool
        except Exception as exc:
            # Invalid user-provided skill should not break the bot.
            self._errors[skill_dir.name] = str(exc)

    def _find_registry_conflict(self, tool_name: str) -> str | None:
        if tool_name in BUILTIN_TOOL_NAMES:
            return f"Tool name {tool_name} conflicts with built-in tools"
        if tool_name in self._tools:
            return f"Duplicate dynamic tool name: {tool_name}"
        return None

    def get_callable_map(self, allowed_names: set[str] | None = None) -> dict[str, Callable[..., Any]]:
        if allowed_names is None:
            return {name: tool.func for name, tool in self._tools.items()}
        return {name: tool.func for name, tool in self._tools.items() if name in allowed_names}

    def get_ollama_tool_schemas(self, allowed_names: set[str] | None = None) -> list[dict[str, Any]]:
        if allowed_names is None:
            return [tool.to_ollama_schema() for tool in self._tools.values()]
        return [tool.to_ollama_schema() for tool in self._tools.values() if tool.name in allowed_names]

    def status_lines(self) -> list[str]:
        loaded = sorted(self._tools.keys())
        lines = [f"Loaded tools ({len(loaded)}): {', '.join(loaded) if loaded else 'none'}"]
        lines.append(
            "Policy: "
            f"allowlist={','.join(sorted(self._allowlist)) if self._allowlist is not None else '*'}; "
            f"strict_args={self._strict_args}; "
            f"require_integrity={self._require_integrity}; "
            f"require_signature={self._require_signature}; "
            f"timeout={self._timeout_seconds}s"
        )
        if self._errors:
            lines.append(f"Load errors ({len(self._errors)}):")
            for skill_name, error in sorted(self._errors.items()):
                lines.append(f"- {skill_name}: {error}")
        else:
            lines.append("Load errors (0): none")
        return lines

    def _load_tool(self, skill_dir: Path, manifest: dict[str, Any]) -> DynamicTool:
        self._validate_manifest(manifest)
        name = str(manifest["name"])
        description = str(manifest.get("description", f"Dynamic tool: {name}"))
        entrypoint = str(manifest["entrypoint"])
        function_name = str(manifest["function"])
        parameters = manifest.get("schema") or {
            "type": "object",
            "properties": {},
            "required": [],
        }

        module_path = skill_dir / entrypoint
        self._validate_skill_package(skill_dir=skill_dir, module_path=module_path, manifest=manifest)
        func = self._build_sandbox_callable(
            tool_name=name,
            module_path=module_path,
            function_name=function_name,
            schema=parameters,
        )

        return DynamicTool(
            name=name,
            description=description,
            parameters=parameters,
            func=func,
            skill_dir=skill_dir,
        )

    def _validate_manifest(self, manifest: dict[str, Any]) -> None:
        required_fields = ("name", "entrypoint", "function")
        missing = [field for field in required_fields if field not in manifest]
        if missing:
            raise ValueError(f"Missing manifest fields: {', '.join(missing)}")

        package_version = str(manifest.get("package_version") or "").strip()
        if not package_version:
            raise ValueError("Missing manifest field: package_version")
        if not _SEMVER_RE.match(package_version):
            raise ValueError("package_version must be semantic version, e.g. 1.2.3")

        schema = manifest.get("schema")
        if schema is not None and not isinstance(schema, dict):
            raise ValueError("schema must be an object")

    def _validate_skill_package(self, *, skill_dir: Path, module_path: Path, manifest: dict[str, Any]) -> None:
        if not module_path.exists():
            raise FileNotFoundError(module_path)
        if module_path.suffix.lower() != ".py":
            raise ValueError("entrypoint must be a .py file")

        self._validate_integrity(skill_dir=skill_dir, manifest=manifest)
        self._validate_signature(manifest)

    def _build_sandbox_callable(
        self,
        *,
        tool_name: str,
        module_path: Path,
        function_name: str,
        schema: dict[str, Any],
    ) -> Callable[..., Any]:
        script_path = Path(__file__).with_name("sandbox_runner.py")
        workspace_root = self.skills_root.parent
        src_root = Path(__file__).resolve().parents[2]

        def _sandbox_callable(**kwargs: Any) -> Any:
            normalized_kwargs = self._flatten_nested_arguments(kwargs)
            self._enforce_argument_policy(tool_name=tool_name, schema=schema, arguments=normalized_kwargs)

            payload = {
                "module_path": str(module_path),
                "function": function_name,
                "kwargs": normalized_kwargs,
            }
            input_data = json.dumps(payload, ensure_ascii=True)

            env = os.environ.copy()
            py_path = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{src_root}{os.pathsep}{py_path}" if py_path else str(src_root)

            proc = subprocess.run(
                [sys.executable, str(script_path)],
                input=input_data,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                cwd=str(workspace_root),
                env=env,
                check=False,
            )
            if proc.returncode != 0:
                stderr = (proc.stderr or "").strip()
                stdout = (proc.stdout or "").strip()
                details = stderr or stdout or f"exit code {proc.returncode}"
                raise RuntimeError(f"Sandbox runner failed: {details}")

            try:
                response = json.loads((proc.stdout or "").strip())
            except json.JSONDecodeError as exc:
                raise RuntimeError("Sandbox runner returned invalid JSON") from exc

            if not isinstance(response, dict):
                raise RuntimeError("Sandbox runner returned invalid response shape")
            if not bool(response.get("ok")):
                raise RuntimeError(str(response.get("error") or "Sandbox runner failed"))
            return response.get("result")

        return _sandbox_callable

    def _validate_integrity(self, *, skill_dir: Path, manifest: dict[str, Any]) -> None:
        file_hashes = manifest.get("package_files_sha256")
        if file_hashes is None:
            if self._require_integrity:
                raise ValueError("Missing package_files_sha256 in manifest")
            return

        if not isinstance(file_hashes, dict) or not file_hashes:
            raise ValueError("package_files_sha256 must be a non-empty object")

        for rel_path, expected_hash in file_hashes.items():
            self._validate_integrity_file(
                skill_dir=skill_dir,
                rel_path=rel_path,
                expected_hash=expected_hash,
            )

    def _validate_integrity_file(self, *, skill_dir: Path, rel_path: Any, expected_hash: Any) -> None:
        if not isinstance(rel_path, str) or not rel_path.strip():
            raise ValueError("package_files_sha256 keys must be non-empty strings")
        if not isinstance(expected_hash, str) or not expected_hash.strip():
            raise ValueError(f"Invalid SHA256 value for {rel_path}")

        normalized_rel = rel_path.replace("\\", "/")
        full_path = (skill_dir / normalized_rel).resolve()
        if not full_path.exists() or not full_path.is_file():
            raise ValueError(f"Integrity check failed: file not found: {normalized_rel}")
        try:
            full_path.relative_to(skill_dir.resolve())
        except ValueError as exc:
            raise ValueError(f"Integrity check failed: path escapes skill dir: {normalized_rel}") from exc

        actual_hash = hashlib.sha256(full_path.read_bytes()).hexdigest().lower()
        if actual_hash != expected_hash.lower():
            raise ValueError(f"Integrity check failed for {normalized_rel}: sha256 mismatch")

    def _validate_signature(self, manifest: dict[str, Any]) -> None:
        signature = manifest.get("package_signature")
        algorithm = str(manifest.get("package_signature_algorithm") or "hmac-sha256").strip().lower()
        key = os.getenv("DYNAMIC_SKILL_SIGNING_KEY", "").strip()

        if signature is None:
            if self._require_signature:
                raise ValueError("Missing package_signature in manifest")
            return

        if algorithm != "hmac-sha256":
            raise ValueError("Unsupported package signature algorithm")
        if not key:
            raise ValueError("DYNAMIC_SKILL_SIGNING_KEY is required to verify package_signature")

        if not isinstance(signature, str) or not signature.strip():
            raise ValueError("package_signature must be non-empty string")

        canonical_manifest = dict(manifest)
        canonical_manifest.pop("package_signature", None)
        payload = json.dumps(canonical_manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        expected = hmac.new(key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature.lower()):
            raise ValueError("package_signature verification failed")

    def _enforce_argument_policy(self, *, tool_name: str, schema: dict[str, Any], arguments: dict[str, Any]) -> None:
        encoded = json.dumps(arguments, ensure_ascii=True)
        if len(encoded.encode("utf-8")) > self._max_args_bytes:
            raise ValueError(f"Arguments exceed {self._max_args_bytes} bytes")

        self._validate_schema_value(
            schema=schema,
            value=arguments,
            path=f"{tool_name}.args",
            strict=self._strict_args,
        )

    def _validate_schema_value(self, *, schema: dict[str, Any], value: Any, path: str, strict: bool) -> None:
        schema_type = str(schema.get("type") or "object")
        if schema_type not in _ARG_PRIMITIVE_TYPES:
            raise ValueError(f"Unsupported schema type at {path}: {schema_type}")

        if schema_type == "object":
            self._validate_schema_object(schema=schema, value=value, path=path, strict=strict)
            return

        if schema_type == "array":
            self._validate_schema_array(schema=schema, value=value, path=path, strict=strict)
            return

        if schema_type == "string":
            self._validate_schema_string(schema=schema, value=value, path=path)
            return

        if schema_type == "integer":
            self._validate_integer(value=value, path=path)
            return

        if schema_type == "number":
            self._validate_number(value=value, path=path)
            return

        if schema_type == "boolean":
            self._validate_boolean(value=value, path=path)

    def _validate_schema_object(self, *, schema: dict[str, Any], value: Any, path: str, strict: bool) -> None:
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be object")
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        required_names = [str(item) for item in required]

        for key in required_names:
            if key not in value:
                raise ValueError(f"Missing required argument: {path}.{key}")

        additional_allowed = bool(schema.get("additionalProperties", not strict))
        for arg_key, arg_value in value.items():
            prop_schema = properties.get(arg_key)
            if isinstance(prop_schema, dict):
                self._validate_schema_value(
                    schema=prop_schema,
                    value=arg_value,
                    path=f"{path}.{arg_key}",
                    strict=strict,
                )
                continue
            if not additional_allowed:
                raise ValueError(f"Unexpected argument: {path}.{arg_key}")

    def _validate_schema_array(self, *, schema: dict[str, Any], value: Any, path: str, strict: bool) -> None:
        if not isinstance(value, list):
            raise ValueError(f"{path} must be array")
        item_schema = schema.get("items") if isinstance(schema.get("items"), dict) else None
        if item_schema is None:
            return

        for idx, item in enumerate(value):
            self._validate_schema_value(
                schema=item_schema,
                value=item,
                path=f"{path}[{idx}]",
                strict=strict,
            )

    def _validate_schema_string(self, *, schema: dict[str, Any], value: Any, path: str) -> None:
        if not isinstance(value, str):
            raise ValueError(f"{path} must be string")
        if len(value) > self._max_string_length:
            raise ValueError(f"{path} exceeds max string length ({self._max_string_length})")
        enum = schema.get("enum") if isinstance(schema.get("enum"), list) else None
        if enum is not None and value not in enum:
            raise ValueError(f"{path} must be one of: {', '.join(str(item) for item in enum)}")

    @staticmethod
    def _validate_integer(*, value: Any, path: str) -> None:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{path} must be integer")

    @staticmethod
    def _validate_number(*, value: Any, path: str) -> None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{path} must be number")

    @staticmethod
    def _validate_boolean(*, value: Any, path: str) -> None:
        if not isinstance(value, bool):
            raise ValueError(f"{path} must be boolean")

    @staticmethod
    def _flatten_nested_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
        nested = arguments.get("arguments")
        if not isinstance(nested, dict):
            return arguments

        merged = dict(nested)
        for key, value in arguments.items():
            if key == "arguments":
                continue
            merged[key] = value
        return merged

    @staticmethod
    def _safe_int(raw: str | None, *, default: int) -> int:
        if raw is None:
            return default
        try:
            return int(raw.strip())
        except Exception:
            return default

    @staticmethod
    def _read_bool(name: str, *, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _parse_tool_allowlist(raw: str) -> set[str] | None:
        value = (raw or "").strip()
        if not value or value == "*":
            return None
        items = {part.strip() for part in value.split(",") if part.strip()}
        return items or None

    def _is_tool_allowed(self, name: str) -> bool:
        if self._allowlist is None:
            return True
        return name in self._allowlist
