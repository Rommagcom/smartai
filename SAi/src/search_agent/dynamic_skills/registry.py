from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable


BUILTIN_TOOL_NAMES = {"web_search", "web_fetch"}


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

            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                tool = self._load_tool(skill_dir, manifest)
            except Exception as exc:
                # Invalid user-provided skill should not break the bot.
                self._errors[skill_dir.name] = str(exc)
                continue

            if tool.name in BUILTIN_TOOL_NAMES:
                self._errors[skill_dir.name] = f"Tool name {tool.name} conflicts with built-in tools"
                continue

            if tool.name in self._tools:
                self._errors[skill_dir.name] = f"Duplicate dynamic tool name: {tool.name}"
                continue

            self._tools[tool.name] = tool

        return self._tools

    def get_callable_map(self) -> dict[str, Callable[..., Any]]:
        return {name: tool.func for name, tool in self._tools.items()}

    def get_ollama_tool_schemas(self) -> list[dict[str, Any]]:
        return [tool.to_ollama_schema() for tool in self._tools.values()]

    def status_lines(self) -> list[str]:
        loaded = sorted(self._tools.keys())
        lines = [f"Loaded tools ({len(loaded)}): {', '.join(loaded) if loaded else 'none'}"]
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
        module = self._load_module(module_path, name)
        func = getattr(module, function_name)
        if not callable(func):
            raise TypeError(f"Function {function_name} is not callable")

        return DynamicTool(
            name=name,
            description=description,
            parameters=parameters,
            func=func,
            skill_dir=skill_dir,
        )

    @staticmethod
    def _load_module(module_path: Path, module_name: str) -> ModuleType:
        if not module_path.exists():
            raise FileNotFoundError(module_path)

        unique_name = f"dynamic_skill_{module_name}_{abs(hash(str(module_path)))}"
        spec = importlib.util.spec_from_file_location(unique_name, module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load module from {module_path}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _validate_manifest(manifest: dict[str, Any]) -> None:
        required_fields = ("name", "entrypoint", "function")
        missing = [field for field in required_fields if field not in manifest]
        if missing:
            raise ValueError(f"Missing manifest fields: {', '.join(missing)}")

        schema = manifest.get("schema")
        if schema is not None and not isinstance(schema, dict):
            raise ValueError("schema must be an object")
