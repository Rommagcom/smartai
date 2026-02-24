from __future__ import annotations

from copy import deepcopy
from typing import Any

PERMISSION_NETWORK_HTTP_READ = "network.http.read"
PERMISSION_INTEGRATIONS_WRITE = "integrations.write"
PERMISSION_INTEGRATIONS_READ = "integrations.read"


class SkillsRegistryService:
    REGISTRY_VERSION = "1.0.0"

    def __init__(self) -> None:
        self._skills: list[dict] = self._build_default_skills()

    @staticmethod
    def _build_default_skills() -> list[dict]:
        return [
            {
                "manifest": {
                    "name": "web_search",
                    "title": "Web Search",
                    "description": "Поиск по вебу через search backend",
                    "version": "1.0.0",
                },
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "permissions": [PERMISSION_NETWORK_HTTP_READ],
            },
            {
                "manifest": {
                    "name": "web_fetch",
                    "title": "Web Fetch",
                    "description": "Получение содержимого URL",
                    "version": "1.0.0",
                },
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "max_chars": {"type": "integer", "minimum": 1000, "maximum": 50000},
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
                "permissions": [PERMISSION_NETWORK_HTTP_READ],
            },
            {
                "manifest": {
                    "name": "browser",
                    "title": "Browser Action",
                    "description": "Browser extract/screenshot/pdf",
                    "version": "1.0.0",
                },
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "action": {"type": "string", "enum": ["extract_text", "screenshot", "pdf"]},
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
                "permissions": [PERMISSION_NETWORK_HTTP_READ, "browser.automation"],
            },
            {
                "manifest": {
                    "name": "pdf_create",
                    "title": "PDF Create",
                    "description": "Генерация PDF из текста",
                    "version": "1.0.0",
                },
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                        "filename": {"type": "string"},
                    },
                    "required": ["content"],
                    "additionalProperties": False,
                },
                "permissions": ["files.generate"],
            },
            {
                "manifest": {
                    "name": "execute_python",
                    "title": "Execute Python",
                    "description": "Выполнение python-кода в sandbox",
                    "version": "1.0.0",
                },
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                    },
                    "required": ["code"],
                    "additionalProperties": False,
                },
                "permissions": ["sandbox.python.execute"],
            },
            {
                "manifest": {
                    "name": "memory_add",
                    "title": "Memory Add",
                    "description": "Добавление факта в memory",
                    "version": "1.0.0",
                },
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "fact_type": {"type": "string"},
                        "content": {"type": "string"},
                        "importance_score": {"type": "number", "minimum": 0, "maximum": 1},
                        "expiration_date": {"type": "string"},
                        "is_pinned": {"type": "boolean"},
                        "is_locked": {"type": "boolean"},
                    },
                    "required": ["content"],
                    "additionalProperties": False,
                },
                "permissions": ["memory.write"],
            },
            {
                "manifest": {
                    "name": "memory_list",
                    "title": "Memory List",
                    "description": "Список memory-фактов",
                    "version": "1.0.0",
                },
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                "permissions": ["memory.read"],
            },
            {
                "manifest": {
                    "name": "memory_search",
                    "title": "Memory Search",
                    "description": "Поиск по memory",
                    "version": "1.0.0",
                },
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "permissions": ["memory.read"],
            },
            {
                "manifest": {
                    "name": "doc_search",
                    "title": "Document Search",
                    "description": "RAG-поиск по загруженным документам",
                    "version": "1.0.0",
                },
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "permissions": ["documents.read"],
            },
            {
                "manifest": {
                    "name": "cron_add",
                    "title": "Cron Add",
                    "description": "Создание cron/reminder задачи",
                    "version": "1.0.0",
                },
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "cron_expression": {"type": "string"},
                        "schedule_text": {"type": "string"},
                        "action_type": {"type": "string"},
                        "payload": {"type": "object"},
                        "task_text": {"type": "string"},
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
                "permissions": ["schedule.write"],
            },
            {
                "manifest": {
                    "name": "cron_list",
                    "title": "Cron List",
                    "description": "Список cron/reminder задач",
                    "version": "1.0.0",
                },
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                "permissions": ["schedule.read"],
            },
            {
                "manifest": {
                    "name": "cron_delete",
                    "title": "Cron Delete",
                    "description": "Удаление cron/reminder задачи",
                    "version": "1.0.0",
                },
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                    },
                    "required": ["job_id"],
                    "additionalProperties": False,
                },
                "permissions": ["schedule.write"],
            },
            {
                "manifest": {
                    "name": "worker_enqueue",
                    "title": "Worker Enqueue",
                    "description": "Постановка задачи в фоновую очередь",
                    "version": "1.0.0",
                },
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "job_type": {"type": "string", "enum": ["web_search", "web_fetch", "pdf_create"]},
                        "payload": {"type": "object"},
                    },
                    "required": ["job_type", "payload"],
                    "additionalProperties": False,
                },
                "permissions": ["worker.enqueue"],
            },
            {
                "manifest": {
                    "name": "integration_onboarding_connect",
                    "title": "Integration Onboarding Connect",
                    "description": "Подготовка onboarding-draft для подключения интеграции",
                    "version": "1.0.0",
                },
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "service_name": {"type": "string"},
                        "token": {"type": "string"},
                        "base_url": {"type": "string"},
                        "endpoints": {"type": "array"},
                        "healthcheck": {"type": "object"},
                    },
                    "required": ["service_name"],
                    "additionalProperties": False,
                },
                "permissions": [PERMISSION_INTEGRATIONS_WRITE],
            },
            {
                "manifest": {
                    "name": "integration_onboarding_test",
                    "title": "Integration Onboarding Test",
                    "description": "Тестирование onboarding-draft интеграции",
                    "version": "1.0.0",
                },
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "draft_id": {"type": "string"},
                        "draft": {"type": "object"},
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                "permissions": [PERMISSION_INTEGRATIONS_WRITE, PERMISSION_NETWORK_HTTP_READ],
            },
            {
                "manifest": {
                    "name": "integration_onboarding_save",
                    "title": "Integration Onboarding Save",
                    "description": "Сохранение onboarding-draft как интеграции",
                    "version": "1.0.0",
                },
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "draft_id": {"type": "string"},
                        "draft": {"type": "object"},
                        "is_active": {"type": "boolean"},
                        "require_successful_test": {"type": "boolean"},
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                "permissions": [PERMISSION_INTEGRATIONS_WRITE],
            },
            {
                "manifest": {
                    "name": "integration_health",
                    "title": "Integration Health",
                    "description": "Проверка health сохраненной интеграции",
                    "version": "1.0.0",
                },
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "integration_id": {"type": "string"},
                    },
                    "required": ["integration_id"],
                    "additionalProperties": False,
                },
                "permissions": [PERMISSION_INTEGRATIONS_READ, PERMISSION_NETWORK_HTTP_READ],
            },
            {
                "manifest": {
                    "name": "integration_add",
                    "title": "Integration Add",
                    "description": "Подключение внешнего API",
                    "version": "1.0.0",
                },
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "service_name": {"type": "string"},
                        "token_optional": {"type": "string"},
                        "base_url_optional": {"type": "string"},
                        "endpoints_optional": {"type": "array"},
                    },
                    "required": ["service_name"],
                    "additionalProperties": False,
                },
                "permissions": [PERMISSION_INTEGRATIONS_WRITE],
            },
            {
                "manifest": {
                    "name": "integrations_list",
                    "title": "Integrations List",
                    "description": "Список подключенных интеграций",
                    "version": "1.0.0",
                },
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                "permissions": [PERMISSION_INTEGRATIONS_READ],
            },
            {
                "manifest": {
                    "name": "integration_call",
                    "title": "Integration Call",
                    "description": "Вызов endpoint подключенной интеграции",
                    "version": "1.0.0",
                },
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "integration_id": {"type": "string"},
                        "url": {"type": "string"},
                        "method": {"type": "string"},
                        "payload": {"type": "object"},
                        "headers": {"type": "object"},
                    },
                    "required": ["integration_id", "url", "method"],
                    "additionalProperties": False,
                },
                "permissions": ["integrations.call", PERMISSION_NETWORK_HTTP_READ, "network.http.write"],
            },
        ]

    def list_contracts(self) -> list[dict]:
        return deepcopy(self._skills)

    def get_contract(self, skill_name: str) -> dict | None:
        target = str(skill_name or "").strip()
        if not target:
            return None
        for item in self._skills:
            name = str(item.get("manifest", {}).get("name") or "").strip()
            if name == target:
                return item
        return None

    def validate_input(self, skill_name: str, payload: dict) -> str | None:
        contract = self.get_contract(skill_name)
        if not contract:
            return f"Unknown skill: {skill_name}"

        schema = contract.get("input_schema")
        if not isinstance(schema, dict):
            return None

        if schema.get("type") == "object" and not isinstance(payload, dict):
            return "Arguments must be an object"

        properties = self._schema_properties(schema)
        required = self._schema_required(schema)

        missing_error = self._validate_required(required, payload)
        if missing_error:
            return missing_error

        extra_error = self._validate_additional(schema=schema, properties=properties, payload=payload)
        if extra_error:
            return extra_error

        for key, value in payload.items():
            prop_schema = properties.get(key)
            if not isinstance(prop_schema, dict):
                continue
            error = self._validate_property(key=key, value=value, schema=prop_schema)
            if error:
                return error

        return None

    @staticmethod
    def _validate_property(key: str, value: Any, schema: dict) -> str | None:
        type_error = SkillsRegistryService._validate_type(key=key, value=value, expected_type=str(schema.get("type") or "").strip())
        if type_error:
            return type_error

        enum_error = SkillsRegistryService._validate_enum(key=key, value=value, enum_values=schema.get("enum"))
        if enum_error:
            return enum_error

        numeric_error = SkillsRegistryService._validate_numeric_bounds(key=key, value=value, schema=schema)
        if numeric_error:
            return numeric_error

        return None

    @staticmethod
    def _schema_properties(schema: dict) -> dict:
        return schema.get("properties") if isinstance(schema.get("properties"), dict) else {}

    @staticmethod
    def _schema_required(schema: dict) -> list:
        return schema.get("required") if isinstance(schema.get("required"), list) else []

    @staticmethod
    def _validate_required(required: list, payload: dict) -> str | None:
        for key in required:
            if str(key) not in payload:
                return f"Missing required argument: {key}"
        return None

    @staticmethod
    def _validate_additional(schema: dict, properties: dict, payload: dict) -> str | None:
        additional_properties = bool(schema.get("additionalProperties", True))
        if additional_properties:
            return None
        unknown = [k for k in payload.keys() if k not in properties]
        if not unknown:
            return None
        unknown_list = ", ".join(sorted(str(k) for k in unknown))
        return f"Unsupported arguments: {unknown_list}"

    @staticmethod
    def _validate_type(key: str, value: Any, expected_type: str) -> str | None:
        if not expected_type:
            return None

        validators = {
            "string": lambda v: isinstance(v, str),
            "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "boolean": lambda v: isinstance(v, bool),
            "object": lambda v: isinstance(v, dict),
            "array": lambda v: isinstance(v, list),
        }
        validator = validators.get(expected_type)
        if validator is None:
            return None
        if validator(value):
            return None
        return f"Argument '{key}' must be {expected_type}"

    @staticmethod
    def _validate_enum(key: str, value: Any, enum_values: Any) -> str | None:
        if not isinstance(enum_values, list):
            return None
        if value in enum_values:
            return None
        return f"Argument '{key}' must be one of: {', '.join(str(item) for item in enum_values)}"

    @staticmethod
    def _validate_numeric_bounds(key: str, value: Any, schema: dict) -> str | None:
        if not (isinstance(value, (int, float)) and not isinstance(value, bool)):
            return None
        min_value = schema.get("minimum")
        max_value = schema.get("maximum")
        if isinstance(min_value, (int, float)) and value < min_value:
            return f"Argument '{key}' must be >= {min_value}"
        if isinstance(max_value, (int, float)) and value > max_value:
            return f"Argument '{key}' must be <= {max_value}"
        return None

    def tool_names(self) -> set[str]:
        return {str(item.get("manifest", {}).get("name") or "").strip() for item in self._skills}

    def planner_signatures(self) -> str:
        signatures: list[str] = []
        for item in self._skills:
            manifest = item.get("manifest", {})
            schema = item.get("input_schema", {})
            name = str(manifest.get("name") or "").strip()
            props = schema.get("properties") if isinstance(schema, dict) else None
            if not name:
                continue
            if not isinstance(props, dict) or not props:
                signatures.append(f"{name}()")
                continue
            args = ", ".join(str(key) for key in props.keys())
            signatures.append(f"{name}({args})")
        return ", ".join(signatures)


skills_registry_service = SkillsRegistryService()
