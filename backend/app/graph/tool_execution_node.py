from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.graph.node_helpers import extract_artifacts
from app.schemas.graph import RouterOutput, ToolResult

logger = logging.getLogger(__name__)


def _dev_log(event: str, **ctx: Any) -> None:
    if not settings.DEV_VERBOSE_LOGGING:
        return
    logger.info(
        f"graph node: {event}",
        extra={"context": {"component": "langgraph", "event": event, **ctx}},
    )


def _build_fallback_export_content(state: dict) -> str:
    history_messages = state.get("history_messages") or []
    for item in reversed(history_messages):
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "").lower() != "assistant":
            continue
        content = str(item.get("content") or "").strip()
        if content:
            return content
    return str(state.get("user_message") or "").strip()


def _build_tool_results(raw_results: list[dict]) -> list[ToolResult]:
    return [
        ToolResult(
            tool=result.get("tool", ""),
            arguments=result.get("arguments", {}),
            success=bool(result.get("success")),
            result=result.get("result") if isinstance(result.get("result"), dict) else None,
            error=result.get("error"),
        )
        for result in raw_results
    ]


async def tool_execution_node(state: dict) -> dict:
    """Execute the planned tool chain from the router output."""
    from app.db.session import AsyncSessionLocal
    from app.models.user import User
    from app.services.tool_orchestrator_service import tool_orchestrator_service

    router_output: RouterOutput | None = state.get("router_output")
    if not router_output or not router_output.steps:
        return {"tool_results": [], "next_step": "chat"}

    user_id = state["user_id"]
    _dev_log(
        "tool_exec_start",
        user_id=str(user_id),
        steps=[step.tool for step in router_output.steps],
    )

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if not user:
                return {
                    "tool_results": [],
                    "error": "User not found",
                    "next_step": "chat",
                }

            steps_dicts = [
                {"tool": step.tool, "arguments": step.arguments}
                for step in router_output.steps
            ]
            raw_results = await tool_orchestrator_service.execute_tool_chain(
                db=db,
                user=user,
                steps=steps_dicts,
                max_steps=settings.LANGGRAPH_MAX_ITERATIONS,
                initial_context={"_fallback_export_content": _build_fallback_export_content(state)},
            )
            await db.commit()
    except Exception as exc:
        logger.warning("tool_execution_node failed: %s", exc)
        return {
            "tool_results": [
                ToolResult(
                    tool="system_error",
                    arguments={},
                    success=False,
                    error="Tool execution unavailable",
                )
            ],
            "error": str(exc),
            "next_step": "compose",
        }

    tool_results = _build_tool_results(raw_results)
    artifacts = extract_artifacts(raw_results)
    _dev_log(
        "tool_exec_done",
        success_count=sum(1 for item in tool_results if item.success),
        total_count=len(tool_results),
    )
    return {
        "tool_results": tool_results,
        "artifacts": artifacts,
        "tool_calls_log": raw_results,
        "next_step": "compose",
    }