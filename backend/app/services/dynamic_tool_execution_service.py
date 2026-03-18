"""Dynamic Tool Execution Service — CRUD, discovery, and runtime execution.

Handles the lifecycle of dynamic tools in use:
- CRUD operations (create, list, delete)
- Discovery for planner and LLM integration
- Execution (HTTP calls + Python skill invocation)
"""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dynamic_tool import DynamicTool
from app.models.user import User
from app.services.auth_data_security_service import auth_data_security_service
from app.services.egress_policy_service import egress_policy_service
from app.services.dynamic_skill_package_service import dynamic_skill_package_service
from app.services.python_skill_runtime import python_skill_runtime

logger = logging.getLogger(__name__)


def _normalize_tool_name(name: str) -> str:
    """Normalize tool name to snake_case."""
    return str(name or "").removeprefix("dyn:").removeprefix("dyn_").strip().lower().replace("-", "_").replace(" ", "_")


def _candidate_tool_names(name: str) -> list[str]:
    """Generate candidate tool names for lookup."""
    from app.services.dynamic_tool_registration_service import _candidate_tool_names as _gen_candidates
    return _gen_candidates(name)


class DynamicToolExecutionService:
    """CRUD, discovery, and execution for dynamic tools."""

    @staticmethod
    def _python_skill_filter():
        """Filter for Python skills (method='python' or endpoint like 'python://')."""
        return or_(
            func.lower(DynamicTool.method) == "python",
            func.lower(DynamicTool.endpoint).like("python://%"),
        )

    @classmethod
    def _apply_kind_filter(cls, query: Any, kind: str | None) -> Any:
        """Apply kind filter to query (python_skill or api_tool)."""
        if kind == "python_skill":
            return query.where(cls._python_skill_filter())
        if kind == "api_tool":
            return query.where(~cls._python_skill_filter())
        return query

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
        """Create a dynamic tool manually (not via registration).

        Validates:
        - Name format (snake_case, no special chars)
        - URL format (via egress policy)
        - Auth token encryption

        Args:
            db: Database session
            user_id: User identifier
            name: Tool name
            description: Short description
            endpoint: API endpoint URL
            method: HTTP method (GET, POST, etc.)
            headers: HTTP headers dict
            auth_token: Optional auth token (will be encrypted)
            parameters_schema: JSON Schema for parameters
            response_hint: Expected response format hint

        Returns:
            Persisted DynamicTool instance

        Raises:
            ValueError: If name or endpoint is invalid
        """
        from app.services.dynamic_tool_registration_service import SAFE_NAME_RE

        name = name.strip().lower().replace("-", "_").replace(" ", "_")
        if not SAFE_NAME_RE.match(name):
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
        kind: str | None = None,
    ) -> list[DynamicTool]:
        """List tools for user."""
        q = select(DynamicTool).where(DynamicTool.user_id == user_id)
        if active_only:
            q = q.where(DynamicTool.is_active.is_(True))
        q = self._apply_kind_filter(q, kind)
        q = q.order_by(DynamicTool.created_at.desc())
        result = await db.execute(q)
        return list(result.scalars().all())

    async def delete_tool(
        self,
        db: AsyncSession,
        user_id: UUID,
        tool_id: UUID,
    ) -> bool:
        """Delete tool by ID.

        Requires admin privileges. Cleans up storage and vector index.

        Args:
            db: Database session
            user_id: User identifier
            tool_id: Tool ID to delete

        Returns:
            True if deleted, False if not found or unauthorized
        """
        user_row = (
            await db.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
        if not user_row or not bool(getattr(user_row, "is_admin", False)):
            return False

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
        dynamic_skill_package_service.cleanup_tool_storage(tool)
        await db.delete(tool)
        await db.commit()
        # Remove from Milvus
        try:
            from app.services.vector_tool_registry import vector_tool_registry
            vector_tool_registry.delete_tool(user_id=str(user_id), tool_name=f"dyn:{tool_name}")
        except Exception as exc:
            logger.debug("failed to delete tool vector: %s", exc)
        return True

    async def delete_tool_by_name(
        self,
        db: AsyncSession,
        user_id: UUID,
        tool_name: str,
        kind: str | None = None,
    ) -> bool:
        """Delete tool by name."""
        clean_name = _normalize_tool_name(tool_name)
        if not clean_name:
            return False

        tool = await self._get_by_name(db, user_id, clean_name, kind=kind)
        if not tool:
            return False
        return await self.delete_tool(db=db, user_id=user_id, tool_id=tool.id)

    async def delete_all_tools(
        self,
        db: AsyncSession,
        user_id: UUID,
        kind: str | None = None,
    ) -> int:
        """Delete all tools for user (requires admin)."""
        user_row = (
            await db.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
        if not user_row or not bool(getattr(user_row, "is_admin", False)):
            return 0

        query = select(DynamicTool).where(DynamicTool.user_id == user_id)
        query = self._apply_kind_filter(query, kind)
        result = await db.execute(query)
        tools = result.scalars().all()
        for t in tools:
            dynamic_skill_package_service.cleanup_tool_storage(t)
            await db.delete(t)
        await db.commit()
        # Remove all from Milvus
        try:
            from app.services.vector_tool_registry import vector_tool_registry
            if kind is None:
                vector_tool_registry.delete_user_tools(user_id=str(user_id))
            else:
                for tool in tools:
                    vector_tool_registry.delete_tool(user_id=str(user_id), tool_name=f"dyn:{tool.name}")
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
        """Return planner-compatible signature block.

        Format: ``dyn:tool_name(param1, param2) — Description, ...``

        Args:
            db: Database session
            user_id: User identifier

        Returns:
            Comma-separated tool signatures for planner injection
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

        Can be passed directly to LiteLLM's ``tools`` parameter.

        Args:
            db: Database session
            user_id: User identifier

        Returns:
            List of function objects for LLM tool use
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
    # Execution — call the registered API or Python skill
    # ------------------------------------------------------------------ #

    async def call_dynamic_tool(
        self,
        db: AsyncSession,
        user_id: UUID,
        tool_name: str,
        arguments: dict,
    ) -> dict:
        """Execute a dynamic tool (HTTP API or Python skill).

        Routes to appropriate executor based on tool type.

        Args:
            db: Database session
            user_id: User identifier
            tool_name: Tool name (will be normalized)
            arguments: Tool parameters

        Returns:
            Result dict with status and output/error
        """
        from app.services.dynamic_python_skill_runtime import call_dynamic_tool

        return await call_dynamic_tool(
            self,
            db=db,
            user_id=user_id,
            tool_name=tool_name,
            arguments=arguments,
        )

    async def call_python_skill(
        self,
        user_id: UUID,
        tool: DynamicTool,
        arguments: dict,
    ) -> dict:
        """Execute a Python skill directly.

        Args:
            user_id: User identifier
            tool: DynamicTool model instance (Python skill)
            arguments: Skill parameters

        Returns:
            Result dict from skill execution
        """
        from app.services.dynamic_python_skill_runtime import call_python_skill

        return await call_python_skill(self, user_id=user_id, tool=tool, arguments=arguments)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    async def _get_by_name(
        db: AsyncSession,
        user_id: UUID,
        name: str,
        kind: str | None = None,
    ) -> DynamicTool | None:
        """Look up tool by name (with candidates: with/without python:// prefix)."""
        candidates = _candidate_tool_names(name)
        if not candidates:
            return None
        query = select(DynamicTool).where(
            DynamicTool.user_id == user_id,
            or_(
                func.lower(DynamicTool.name).in_(candidates),
                func.lower(DynamicTool.endpoint).in_(candidates),
            ),
        )
        query = DynamicToolExecutionService._apply_kind_filter(query, kind)
        result = await db.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    def _extract_param_names(schema: dict) -> list[str]:
        """Extract parameter names from JSON Schema properties."""
        if not isinstance(schema, dict):
            return []
        properties = schema.get("properties")
        if isinstance(properties, dict):
            return list(properties.keys())
        # Fallback: if schema IS properties dict directly
        if schema and "type" not in schema:
            return list(schema.keys())
        return []


dynamic_tool_execution_service = DynamicToolExecutionService()
