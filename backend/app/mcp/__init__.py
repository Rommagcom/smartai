"""MCP (Model Context Protocol) server — exposes assistant tools as MCP resources.

This makes the SmartAi system compatible with the Anthropic MCP ecosystem
and allows external clients to discover and invoke our tools via the
standardized MCP protocol.

The server exposes:
  - Tool registry as MCP tools
  - Memory as MCP resources
  - Chat as MCP prompts
"""
from __future__ import annotations

import logging
from typing import Any

from mcp.server import Server
from mcp.types import (
    Resource,
    TextContent,
    Tool,
)

logger = logging.getLogger(__name__)

# Create MCP server instance
mcp_server = Server("smartai-assistant")


# ---------------------------------------------------------------------------
# Tool discovery — expose registered skills as MCP tools
# ---------------------------------------------------------------------------


@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    """Return all available tools in MCP format."""
    from app.services.skills_registry_service import skills_registry_service

    tools: list[Tool] = []
    for skill in skills_registry_service.list_skills():
        manifest = skill.get("manifest", {})
        input_schema = skill.get("input_schema", {})
        tools.append(Tool(
            name=manifest.get("name", ""),
            description=manifest.get("description", ""),
            inputSchema=input_schema,
        ))
    return tools


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any] | None = None) -> list[TextContent]:
    """Execute a tool via MCP protocol.

    This bridges MCP tool calls to our existing tool_orchestrator_service.
    Note: requires a valid user context, which in MCP mode uses a service account.
    """
    import json
    from app.db.session import AsyncSessionLocal
    from app.services.tool_orchestrator_service import tool_orchestrator_service
    from app.models.user import User
    from sqlalchemy import select

    args = arguments or {}

    async with AsyncSessionLocal() as db:
        # In MCP mode, use the first admin user as context
        # In production, this should use proper MCP authentication
        result = await db.execute(
            select(User).where(User.is_admin.is_(True)).limit(1)
        )
        user = result.scalar_one_or_none()
        if not user:
            return [TextContent(type="text", text="No admin user available for MCP context")]

        tool_results = await tool_orchestrator_service.execute_tool_chain(
            db=db,
            user=user,
            steps=[{"tool": name, "arguments": args}],
            max_steps=1,
        )
        await db.commit()

    result_text = json.dumps(tool_results, ensure_ascii=False, default=str)
    return [TextContent(type="text", text=result_text)]


# ---------------------------------------------------------------------------
# Resource discovery — expose memory as MCP resources
# ---------------------------------------------------------------------------


_MIME_JSON = "application/json"


@mcp_server.list_resources()
async def list_resources() -> list[Resource]:
    """List available MCP resources."""
    return [
        Resource(
            uri="smartai://memory/long-term",
            name="Long-term Memory",
            description="User's long-term memory facts (pgvector)",
            mimeType=_MIME_JSON,
        ),
        Resource(
            uri="smartai://memory/short-term",
            name="Short-term Memory",
            description="Current session context (Redis STM)",
            mimeType=_MIME_JSON,
        ),
        Resource(
            uri="smartai://tools/registry",
            name="Tool Registry",
            description="Available tool definitions and schemas",
            mimeType=_MIME_JSON,
        ),
    ]


# ---------------------------------------------------------------------------
# Initialization helper
# ---------------------------------------------------------------------------


def get_mcp_server() -> Server:
    """Return the configured MCP server instance."""
    return mcp_server
