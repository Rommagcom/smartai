"""Python Skill Runtime — execution context, logging, and sandbox support.

Handles:
- Runtime context building for Python skill execution
- Execution event logging (in-memory + persistence to audit DB)
- Runner context with capabilities and sandboxing metadata
"""

import logging
from typing import Any
from uuid import UUID

from app.services.dynamic_python_skill_support import build_runner_context

logger = logging.getLogger(__name__)


class PythonSkillRuntime:
    """Runtime support for Python dynamic skills."""

    @staticmethod
    def log_skill_execution_event(
        *,
        event: str,
        user_id: UUID,
        tool_name: str,
        execution_mode: str,
        success: bool | None = None,
        error: str | None = None,
    ) -> None:
        """Log skill execution event to logger (dev mode).

        Args:
            event: Event type (e.g., 'skill_execution_start', 'skill_execution_complete')
            user_id: User identifier
            tool_name: Dynamic tool name
            execution_mode: 'sandbox' or 'offline'
            success: Whether execution succeeded (None if not terminal state)
            error: Error message if failed (truncated to 500 chars)
        """
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
    async def persist_skill_execution_event(
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
        """Persist skill execution event to audit database.

        Wraps dynamic_skill_audit_service with graceful error handling.
        Failures in audit persistence do not block execution.

        Args:
            event: Event type
            user_id: User identifier
            tool_name: Dynamic tool name
            execution_mode: 'sandbox' or 'offline'
            execution_id: Execution trace ID
            success: Execution result
            error: Error message
            payload: Additional event metadata
        """
        try:
            from app.services.dynamic_skill_audit_service import dynamic_skill_audit_service

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

    @staticmethod
    def build_python_skill_context(
        *,
        user_id: UUID,
        tool_name: str,
        capabilities: dict,
    ) -> dict:
        """Build execution context for Python skill runner.

        Includes user metadata, tool identity, and available capabilities.

        Args:
            user_id: User identifier
            tool_name: Dynamic tool name
            capabilities: Skills that tool can execute (from manifest)

        Returns:
            Context dict with user_id, tool_name, capabilities, execution_id
        """
        from app.services.dynamic_python_skill_support import build_python_skill_context as build_ctx

        return build_ctx(user_id=user_id, tool_name=tool_name, capabilities=capabilities)

    @staticmethod
    def build_runner_context(
        *,
        user_id: UUID,
        tool_name: str,
        capabilities: dict,
        execution_id: str,
    ) -> dict[str, Any]:
        """Build complete runner context for sandbox execution.

        Combines Python skill context with execution ID for tracing/logging.

        Args:
            user_id: User identifier
            tool_name: Dynamic tool name
            capabilities: Available capabilities
            execution_id: Unique execution trace ID

        Returns:
            Context dict ready for runner invocation
        """
        return build_runner_context(
            user_id=user_id,
            tool_name=tool_name,
            capabilities=capabilities,
            execution_id=execution_id,
        )


python_skill_runtime = PythonSkillRuntime()
