"""Helper functions for router node pipeline.

Extracted router-related functions to avoid circular imports and enable reuse.
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.graph.orchestration_types import SystemToolName
from app.graph.router_recovery import (
    extract_router_output_from_exception,
)
from app.graph.routing_policy import (
    WEB_SEARCH_RE,
    deterministic_route,
    fallback_explicit_export_route,
    fallback_live_data_export_route,
    feedback_requires_web_search,
    feedback_to_search_query,
    followup_export_route,
    is_live_data_query,
    is_web_search_intent,
    strip_web_search_prefix,
)
from app.graph.prompt_routing_policy import (
    hard_structured_route as _hard_structured_route,
)
from app.graph.user_tool_context import load_user_tool_context
from app.schemas.graph import RouterDecision, RouterOutput, ToolStep

logger = logging.getLogger(__name__)


def _dev_log(event: str, **ctx: Any) -> None:
    if not settings.DEV_VERBOSE_LOGGING:
        return
    logger.info(
        f"graph node: {event}",
        extra={"context": {"component": "langgraph", "event": event, **ctx}},
    )


# ============================================================================
# Deterministic Routing (no LLM)
# ============================================================================


def _build_retrieved_tools_block(retrieved_tools: list[dict]) -> str:
    """Format retrieved tools for inclusion in router prompt."""
    if not retrieved_tools:
        return ""

    def _format_hit(hit: dict) -> str | None:
        from app.schemas.tool_registry import RetrievedTool

        try:
            tool = RetrievedTool(**hit)
        except Exception:
            return None
        return f"  - {tool.to_planner_signature()} [score={tool.score:.2f}]"

    lines: list[str] = []
    for hit in retrieved_tools:
        line = _format_hit(hit)
        if line:
            lines.append(line)

    if not lines:
        return ""

    return (
        "\nСемантически найденные инструменты (наиболее релевантны запросу):\n"
        + "\n".join(lines)
        + "\n"
    )


def try_feedback_web_search_route(user_message: str, feedback_plan: str) -> RouterOutput | None:
    """Check if feedback plan requires web search."""
    if not (feedback_plan and feedback_requires_web_search(feedback_plan)):
        return None
    query = feedback_to_search_query(feedback_plan, user_message)
    return RouterOutput(
        decision=RouterDecision.WEB_SEARCH,
        steps=[ToolStep(tool=SystemToolName.WEB_SEARCH.value, arguments={"query": query})],
        response_hint="Следующий цикл: web_search по плану доработки",
        confidence=0.95,
    )


def try_hard_route(user_message: str) -> RouterOutput | None:
    """Try hard-structured (regex-based) routing."""
    hard_route = _hard_structured_route(user_message)
    if hard_route is None:
        return None
    _dev_log("router_hard_structured", steps_count=len(hard_route.steps))
    return hard_route


def try_export_followup_route(user_message: str, history: list[dict]) -> RouterOutput | None:
    """Try export-followup deterministic route."""
    if not settings.ROUTER_ENABLE_EXPORT_FOLLOWUP_SHORTCUT:
        return None
    export_followup = followup_export_route(user_message, history)
    if export_followup is None:
        return None
    _dev_log("router_deterministic_export_followup", decision=export_followup.decision.value)
    return export_followup


def try_deterministic_route(user_message: str) -> RouterOutput | None:
    """Try deterministic (no-LLM) routing."""
    route = deterministic_route(user_message)
    if route is None:
        return None
    _dev_log("router_deterministic", decision=route.decision.value)
    return route


def try_live_data_route(user_message: str) -> RouterOutput | None:
    """Check if request is for live data (weather, stocks, etc)."""
    if not is_live_data_query(user_message):
        return None
    
    # Live data implied web_search
    query = user_message
    if query.startswith("web_search "):
        query = strip_web_search_prefix(query)
    
    return RouterOutput(
        decision=RouterDecision.WEB_SEARCH,
        steps=[ToolStep(tool=SystemToolName.WEB_SEARCH.value, arguments={"query": query})],
        response_hint="Live data request: querying web",
        confidence=0.85,
    )


def try_live_export_override(user_message: str, router_output: RouterOutput) -> RouterOutput | None:
    """Check if live data should override to export path."""
    if router_output.decision != RouterDecision.TOOL:
        return None
    
    live_export = fallback_live_data_export_route(user_message, router_output)
    if live_export is None:
        return None
    
    _dev_log("router_live_data_export_override")
    return live_export


def try_web_search_intent_route(user_message: str) -> RouterOutput | None:
    """Check for explicit web search intent (e.g., 'найди в интернете')."""
    if not is_web_search_intent(user_message):
        return None
    
    query = user_message
    if WEB_SEARCH_RE:
        import re
        match = WEB_SEARCH_RE.search(user_message)
        if match:
            query = match.group(1) if match.groups() else user_message
    
    return RouterOutput(
        decision=RouterDecision.WEB_SEARCH,
        steps=[ToolStep(tool=SystemToolName.WEB_SEARCH.value, arguments={"query": query})],
        response_hint="Explicit web search intent detected",
        confidence=0.9,
    )


def get_router_fallback_export(user_message: str, router_output: RouterOutput) -> RouterOutput | None:
    """Get fallback export path if available."""
    return fallback_explicit_export_route(user_message, router_output)


# ============================================================================
# Recovery Helpers
# ============================================================================


def build_web_search_fallback(user_message: str) -> RouterOutput:
    """Create fallback web_search route when other routing fails."""
    return RouterOutput(
        decision=RouterDecision.WEB_SEARCH,
        steps=[ToolStep(tool=SystemToolName.WEB_SEARCH.value, arguments={"query": user_message})],
        response_hint="Fallback: web_search на исходный вопрос",
        confidence=0.6,
    )


def handle_router_failure(exc: Exception, user_message: str) -> RouterOutput:
    """Handle router LLM failure with recovery."""
    _dev_log("router_llm_failed", error=str(exc)[:100])
    
    # Try to salvage partial output
    salvaged = extract_router_output_from_exception(exc, user_message)
    if salvaged is not None:
        _dev_log("router_json_salvage_success")
        return salvaged
    
    # Fallback to web search
    return build_web_search_fallback(user_message)
