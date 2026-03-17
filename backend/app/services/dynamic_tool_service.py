"""Dynamic Tool Service — unified facade for tool registration, discovery, and execution.

This module consolidates three domain-specific services:
1. DynamicToolRegistrationService — NL and ZIP-based registration
2. DynamicSkillPackageService — ZIP parsing, validation, storage
3. DynamicToolExecutionService — CRUD, discovery, and execution

The original DynamicToolService class provides a unified interface by delegating
to these specialized services, maintaining backward compatibility with existing code.

### Architecture:

```
dynamic_tool_service (singleton)
  ├── registration_service
  │   ├── NL registration (LLM-assisted through helpers)
  │   └── ZIP registration (delegates to package_service)
  ├── package_service
  │   ├── ZIP parsing/validation
  │   ├── Code/manifest validation
  │   └── Filesystem storage
  ├── execution_service
  │   ├── CRUD operations
  │   ├── Discovery (planner, LLM)
  │   └── Execution (HTTP, Python)
    └── runtime_service
            ├── Execution logging (dev mode)
            ├── Audit persistence
            └── Context building

Each service is independently testable and can be used separately if needed.
DynamicToolService provides unified backwards-compatible interface.
```
"""

from app.services.dynamic_tool_execution_service import (
    dynamic_tool_execution_service,
)
from app.services.dynamic_tool_registration_service import (
    META_REGISTRATION_PROMPT,
    dynamic_tool_registration_service,
)
from app.services.dynamic_skill_package_service import dynamic_skill_package_service
from app.services.python_skill_runtime import python_skill_runtime

__all__ = [
    "DynamicToolService",
    "dynamic_tool_service",
    "META_REGISTRATION_PROMPT",
]


class DynamicToolService:
    """Unified facade for dynamic tool service operations.

    Routes requests to specialized services while maintaining
    backward-compatible interface for existing code.
    """

    def __init__(self):
        self.registration_service = dynamic_tool_registration_service
        self.package_service = dynamic_skill_package_service
        self.execution_service = dynamic_tool_execution_service
        self.runtime_service = python_skill_runtime

    # ================================================================
    # Registration API (delegates to registration_service)
    # ================================================================

    async def register_from_user_message(self, db, user_id, user_message):
        """Register tool via natural language description (LLM-assisted)."""
        return await self.registration_service.register_from_user_message(
            db=db,
            user_id=user_id,
            user_message=user_message,
        )

    async def register_skill_package(self, db, user_id, *, filename, content):
        """Register tool via ZIP package."""
        return await self.registration_service.register_skill_package(
            db=db,
            user_id=user_id,
            filename=filename,
            content=content,
        )

    # ================================================================
    # CRUD API (delegates to execution_service)
    # ================================================================

    async def create_tool(
        self,
        db,
        user_id,
        *,
        name,
        description="",
        endpoint,
        method="GET",
        headers=None,
        auth_token=None,
        parameters_schema=None,
        response_hint="",
    ):
        """Create a tool manually (not via registration)."""
        return await self.execution_service.create_tool(
            db=db,
            user_id=user_id,
            name=name,
            description=description,
            endpoint=endpoint,
            method=method,
            headers=headers,
            auth_token=auth_token,
            parameters_schema=parameters_schema,
            response_hint=response_hint,
        )

    async def list_tools(self, db, user_id, active_only=True, kind=None):
        """List user's tools."""
        return await self.execution_service.list_tools(
            db=db,
            user_id=user_id,
            active_only=active_only,
            kind=kind,
        )

    async def delete_tool(self, db, user_id, tool_id):
        """Delete tool by ID."""
        return await self.execution_service.delete_tool(
            db=db,
            user_id=user_id,
            tool_id=tool_id,
        )

    async def delete_tool_by_name(self, db, user_id, tool_name, kind=None):
        """Delete tool by name."""
        return await self.execution_service.delete_tool_by_name(
            db=db,
            user_id=user_id,
            tool_name=tool_name,
            kind=kind,
        )

    async def delete_all_tools(self, db, user_id, kind=None):
        """Delete all tools for user."""
        return await self.execution_service.delete_all_tools(
            db=db,
            user_id=user_id,
            kind=kind,
        )

    # ================================================================
    # Discovery API (delegates to execution_service)
    # ================================================================

    async def get_tools_for_planner(self, db, user_id):
        """Get tool signatures for planner injection."""
        return await self.execution_service.get_tools_for_planner(
            db=db,
            user_id=user_id,
        )

    async def get_tools_for_llm(self, db, user_id):
        """Get tool definitions in OpenAI format for LLM."""
        return await self.execution_service.get_tools_for_llm(
            db=db,
            user_id=user_id,
        )

    # ================================================================
    # Execution API (delegates to execution_service + runtime_service)
    # ================================================================

    async def call_dynamic_tool(self, db, user_id, tool_name, arguments):
        """Execute a dynamic tool (HTTP API or Python skill)."""
        return await self.execution_service.call_dynamic_tool(
            db=db,
            user_id=user_id,
            tool_name=tool_name,
            arguments=arguments,
        )

    async def call_python_skill(self, user_id, tool, arguments):
        """Execute a Python skill."""
        return await self.execution_service.call_python_skill(
            user_id=user_id,
            tool=tool,
            arguments=arguments,
        )

    # ================================================================
    # Logging/Runtime API (backward-compat: some code may call these directly)
    # ================================================================

    def _log_skill_execution_event(
        self,
        *,
        event,
        user_id,
        tool_name,
        execution_mode,
        success=None,
        error=None,
    ):
        """Log skill execution event (in-memory)."""
        return self.runtime_service.log_skill_execution_event(
            event=event,
            user_id=user_id,
            tool_name=tool_name,
            execution_mode=execution_mode,
            success=success,
            error=error,
        )

    async def _persist_skill_execution_event(
        self,
        *,
        event,
        user_id,
        tool_name,
        execution_mode,
        execution_id,
        success=None,
        error=None,
        payload=None,
    ):
        """Persist skill execution event to audit DB."""
        return await self.runtime_service.persist_skill_execution_event(
            event=event,
            user_id=user_id,
            tool_name=tool_name,
            execution_mode=execution_mode,
            execution_id=execution_id,
            success=success,
            error=error,
            payload=payload,
        )

    # ================================================================
    # Storage Cleanup (backward-compat: delegated to package_service)
    # ================================================================

    def _cleanup_tool_storage(self, tool):
        """Clean up filesystem storage for deleted tool."""
        return self.package_service.cleanup_tool_storage(tool)


# ====================================================================
# Singleton export (maintains backward compatibility)
# ====================================================================

dynamic_tool_service = DynamicToolService()
