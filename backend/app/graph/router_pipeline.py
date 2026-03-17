"""Router node pipeline orchestration.

Pipeline stages for intent classification and tool selection:
1. Input extraction
2. Deterministic routing (feedback, hard routes, export followup)
3. Live data detection
4. LLM planner execution
5. Post-processing (overrides, fallbacks)
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.graph.router_helpers import (
    _dev_log,
    build_web_search_fallback,
    get_router_fallback_export,
    handle_router_failure,
    try_deterministic_route,
    try_export_followup_route,
    try_feedback_web_search_route,
    try_hard_route,
    try_live_data_route,
    try_live_export_override,
    try_web_search_intent_route,
)
from app.graph.router_types import RouterInput, RouterResult
from app.graph.user_tool_context import load_user_tool_context
from app.schemas.graph import RouterOutput

logger = logging.getLogger(__name__)


# ============================================================================
# Stage 1: Input Extraction
# ============================================================================


def extract_router_input(state: dict) -> RouterInput:
    """Extract router inputs from graph state."""
    messages: list[str] = state.get("messages") or []
    user_message = (messages[-1] if messages else state.get("user_message", "")).strip()
    history = state.get("history_messages") or []
    feedback_plan = str(state.get("feedback_plan") or "").strip()
    retrieved_tools = state.get("retrieved_tools") or []
    user_id = state.get("user_id")
    session_id = state.get("session_id")
    
    _dev_log(
        "router_extract_input",
        user_msg_len=len(user_message),
        has_feedback=bool(feedback_plan),
        retrieved_tools_count=len(retrieved_tools),
    )
    
    return RouterInput(
        user_message=user_message,
        history=history,
        feedback_plan=feedback_plan,
        retrieved_tools=retrieved_tools,
        user_id=user_id,
        session_id=session_id,
    )


# ============================================================================
# Stage 2: Deterministic Routing
# ============================================================================


def try_deterministic_routes(input_data: RouterInput) -> RouterOutput | None:
    """Try all deterministic (regexes, hard-coded) routing rules.
    
    Returns immediately on first match. Order matters:
    1. Feedback web search (highest priority - from previous iteration)
    2. Hard structured routes (regex-based direct commands)
    3. Export followup (context-aware export shortcuts)
    4. Regular deterministic routing
    
    Args:
        input_data: Extracted router input
    
    Returns:
        RouterOutput if any rule matched, None to continue to LLM
    """
    # 1. Check feedback-driven web search
    if input_data.has_feedback:
        route = try_feedback_web_search_route(input_data.user_message, input_data.feedback_plan)
        if route:
            _dev_log("router_feedback_web_search")
            return route
    
    # 2. Hard structured routing (regex patterns)
    route = try_hard_route(input_data.user_message)
    if route:
        return route
    
    # 3. Export followup (conversation context)
    route = try_export_followup_route(input_data.user_message, input_data.history)
    if route:
        return route
    
    # 4. Regular deterministic routing
    route = try_deterministic_route(input_data.user_message)
    if route:
        return route
    
    # 5. Explicit web search intent
    route = try_web_search_intent_route(input_data.user_message)
    if route:
        _dev_log("router_web_search_intent")
        return route
    
    return None


# ============================================================================
# Stage 3: Live Data Detection
# ============================================================================


def try_live_data(input_data: RouterInput) -> RouterOutput | None:
    """Check for live data requests (weather, stocks, etc).
    
    Live data implies web_search path.
    
    Args:
        input_data: Extracted router input
    
    Returns:
        RouterOutput for web_search if live data detected, None otherwise
    """
    route = try_live_data_route(input_data.user_message)
    if route:
        _dev_log("router_live_data_detected")
    return route


# ============================================================================
# Stage 4: Context Loading & LLM Execution
# ============================================================================


def load_router_context(
    input_data: RouterInput,
) -> tuple[str, str]:
    """Load dynamic tool context (dynamic tools, integrations, retrieved tools).
    
    Args:
        input_data: Extracted router input
    
    Returns:
        Tuple of (integrations_block, dynamic_tools_block) for prompt
    """
    integrations_block, dynamic_tools_block = load_user_tool_context(input_data.user_id)
    
    _dev_log(
        "router_context_loaded",
        integrations_len=len(integrations_block),
        dynamic_tools_len=len(dynamic_tools_block),
    )
    
    return integrations_block, dynamic_tools_block


async def run_router_llm(
    llm_provider: Any,
    user_message: str,
    feedback_plan: str,
    tool_catalog_signatures: str,
    dynamic_tools_block: str,
    integrations_block: str,
    retrieved_block: str,
) -> RouterOutput | None:
    """Execute router LLM call for intent classification and tool selection.
    
    Args:
        llm_provider: LLM provider instance
        user_message: Current user query
        feedback_plan: Feedback from previous iteration
        tool_catalog_signatures: Static tools catalog
        dynamic_tools_block: User's registered dynamic tools
        integrations_block: User's registered integrations
        retrieved_block: Semantically retrieved tools
    
    Returns:
        Structured RouterOutput, or None if LLM fails
    """
    from app.graph.prompt_builders import build_router_prompt
    
    prompt = build_router_prompt(
        user_message=user_message,
        feedback_plan=feedback_plan,
        tool_catalog_signatures=tool_catalog_signatures,
        dynamic_tools_block=dynamic_tools_block,
        integrations_block=integrations_block,
        retrieved_block=retrieved_block,
    )
    
    messages: list[dict[str, str]] = [{"role": "system", "content": prompt}]
    messages.append({"role": "user", "content": user_message})
    
    try:
        _dev_log("router_llm_call_start")
        
        output = await llm_provider.chat_structured(
            messages=messages,
            response_model=RouterOutput,
            model=settings.LITELLM_PLANNER_MODEL or None,
            temperature=0.0,
            max_tokens=int(settings.OLLAMA_NUM_PREDICT_PLANNER),
        )
        
        _dev_log("router_llm_call_done", decision=output.decision.value)
        return output
    
    except Exception:
        return None


# ============================================================================
# Stage 5: Post-processing (Overrides & Fallbacks)
# ============================================================================


def apply_live_data_export_override(
    output: RouterOutput,
    user_message: str,
) -> RouterOutput:
    """Check if live data should override to explicit export path.
    
    Args:
        output: Router output from LLM
        user_message: Original user query
    
    Returns:
        Possibly overridden RouterOutput
    """
    override = try_live_export_override(user_message, output)
    if override:
        _dev_log("router_live_data_export_override")
        return override
    return output


def apply_fallback_export(
    output: RouterOutput,
    user_message: str,
) -> RouterOutput:
    """Check for fallback export path if tool routing failed.
    
    Args:
        output: Router output from LLM
        user_message: Original user query
    
    Returns:
        Possibly adjusted RouterOutput
    """
    fallback = get_router_fallback_export(user_message, output)
    if fallback:
        _dev_log("router_fallback_export_applied")
        return fallback
    return output


# ============================================================================
# Public Entry Point
# ============================================================================


async def run_router_pipeline(state: dict) -> RouterResult:
    """Execute complete router pipeline.
    
    Pipeline stages:
    1. Input extraction
    2. Deterministic routing (feedback, hard routes, etc.)
    3. Live data detection
    4. Context loading & LLM execution
    5. Post-processing (overrides, fallbacks)
    
    Args:
        state: LangGraph state dict
    
    Returns:
        RouterResult with output or error
    """
    from app.llm import llm_provider
    from app.schemas.tool_registry import get_tool_catalog_signatures
    from app.graph.router_helpers import _build_retrieved_tools_block
    
    # Stage 1: Extract input
    input_data = extract_router_input(state)
    
    # Stage 2: Try deterministic routes
    output = try_deterministic_routes(input_data)
    if output:
        return RouterResult.success(output)
    
    # Stage 3: Try live data detection
    output = try_live_data(input_data)
    if output:
        return RouterResult.success(output)
    
    # Stage 4: Load context & run LLM
    integrations_block, dynamic_tools_block = load_router_context(input_data)
    tool_catalog_signatures = get_tool_catalog_signatures()
    retrieved_block = _build_retrieved_tools_block(input_data.retrieved_tools)
    
    try:
        output = await run_router_llm(
            llm_provider=llm_provider,
            user_message=input_data.user_message,
            feedback_plan=input_data.feedback_plan,
            tool_catalog_signatures=tool_catalog_signatures,
            dynamic_tools_block=dynamic_tools_block,
            integrations_block=integrations_block,
            retrieved_block=retrieved_block,
        )
    except Exception:
        output = None
    
    # Handle LLM failure
    if output is None:
        output = handle_router_failure(
            RuntimeError("Router LLM call failed"),
            input_data.user_message,
        )
    
    # Stage 5: Post-process output
    output = apply_live_data_export_override(output, input_data.user_message)
    output = apply_fallback_export(output, input_data.user_message)
    
    _dev_log("router_final_decision", decision=output.decision.value)
    
    return RouterResult.success(output)
