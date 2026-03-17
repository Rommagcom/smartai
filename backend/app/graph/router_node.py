from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.graph.graph_contexts import RouterContext
from app.graph.orchestration_types import (
    FallbackMode,
    NextStep,
    RouterNodeUpdate,
    SystemToolName,
    next_step_from_router_decision,
)
from app.graph.router_helpers import (
    _build_retrieved_tools_block,
    _dev_log,
    build_web_search_fallback,
    try_deterministic_route,
    try_export_followup_route,
    try_feedback_web_search_route,
    try_hard_route,
    try_live_data_route,
    try_live_export_override,
)
from app.graph.router_types import RouterResult
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
from app.graph.router_recovery import (
    extract_router_output_from_exception,
)
from app.graph.user_tool_context import load_user_tool_context
from app.graph.prompt_builders import build_router_prompt as _build_router_prompt
from app.graph.router_decision_policy import (
    decide_fallback_mode,
    extract_router_decision_intent,
    should_skip_router_llm,
)
from app.schemas.graph import RouterDecision, RouterOutput, ToolStep

logger = logging.getLogger(__name__)
_WEB_SEARCH_HINT = "Выполни поиск в интернете"


def _dev_log(event: str, **ctx: Any) -> None:
    if not settings.DEV_VERBOSE_LOGGING:
        return
    logger.info(
        f"graph node: {event}",
        extra={"context": {"component": "langgraph", "event": event, **ctx}},
    )


def _build_retrieved_tools_block(retrieved_tools: list[dict]) -> str:
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


def _try_feedback_web_search_route(user_message: str, feedback_plan: str) -> dict | None:
    if not (feedback_plan and feedback_requires_web_search(feedback_plan)):
        return None
    query = feedback_to_search_query(feedback_plan, user_message)
    reflexion_route = RouterOutput(
        decision=RouterDecision.WEB_SEARCH,
        steps=[ToolStep(tool=SystemToolName.WEB_SEARCH.value, arguments={"query": query})],
        response_hint="Следующий цикл: web_search по плану доработки",
        confidence=0.95,
    )
    return RouterNodeUpdate(
        router_output=reflexion_route,
        next_step=NextStep.WEB_SEARCH,
    ).to_state_update()


def _try_hard_route(user_message: str) -> dict | None:
    hard_route = _hard_structured_route(user_message)
    if hard_route is None:
        return None
    _dev_log("router_hard_structured", steps_count=len(hard_route.steps))
    return RouterNodeUpdate(
        router_output=hard_route,
        next_step=next_step_from_router_decision(hard_route.decision),
    ).to_state_update()


def _try_export_followup_route(user_message: str, history: list[dict]) -> dict | None:
    if not settings.ROUTER_ENABLE_EXPORT_FOLLOWUP_SHORTCUT:
        return None
    export_followup = followup_export_route(user_message, history)
    if export_followup is None:
        return None
    _dev_log("router_deterministic_export_followup", decision=export_followup.decision.value)
    return RouterNodeUpdate(
        router_output=export_followup,
        next_step=next_step_from_router_decision(export_followup.decision),
    ).to_state_update()


def _try_deterministic_route(user_message: str) -> dict | None:
    if not settings.ROUTER_ENABLE_DETERMINISTIC_SHORTCUTS:
        return None
    deterministic = deterministic_route(user_message)
    if deterministic is None:
        return None
    _dev_log("router_deterministic", decision=deterministic.decision.value)
    return RouterNodeUpdate(
        router_output=deterministic,
        next_step=next_step_from_router_decision(deterministic.decision),
    ).to_state_update()


async def _load_router_context(user_id: Any) -> tuple[str, str]:
    if not user_id:
        return "", ""
    return await load_user_tool_context(user_id)


def _try_live_data_route(
    *,
    user_id: Any,
    user_message: str,
    integrations_block: str,
    dynamic_tools_block: str,
) -> dict | None:
    if not (
        user_id
        and not integrations_block.strip()
        and not dynamic_tools_block.strip()
        and is_live_data_query(user_message)
    ):
        return None
    query = user_message.strip()
    live_data_route = RouterOutput(
        decision=RouterDecision.WEB_SEARCH,
        steps=[ToolStep(tool=SystemToolName.WEB_SEARCH.value, arguments={"query": query})],
        response_hint="Быстрый путь live-data: web_search без planner LLM",
        confidence=0.9,
    )
    _dev_log("router_fast_live_data", query=query[:120])
    return RouterNodeUpdate(
        router_output=live_data_route,
        next_step=NextStep.WEB_SEARCH,
    ).to_state_update()


def _build_router_messages(*, planner_prompt: str, history: list[dict], user_message: str) -> list[dict[str, str]]:
    router_messages: list[dict[str, str]] = [{"role": "system", "content": planner_prompt}]
    recent = history[-settings.CONTEXT_ALWAYS_KEEP_LAST_MESSAGES:]
    if recent:
        router_messages.extend(recent)
    router_messages.append({"role": "user", "content": user_message})
    return router_messages


def _try_live_export_override(user_message: str, router_output: RouterOutput) -> dict | None:
    if not (
        settings.ROUTER_OVERRIDE_CLARIFY_LIVE_EXPORT
        and settings.ROUTER_ENABLE_LIVE_EXPORT_FALLBACK
        and router_output.decision in {RouterDecision.CLARIFY, RouterDecision.CHAT}
    ):
        return None
    live_export_override = fallback_live_data_export_route(user_message)
    if live_export_override is None:
        return None
    _dev_log(
        "router_override_live_export",
        original_decision=router_output.decision.value,
        steps_count=len(live_export_override.steps),
    )
    return RouterNodeUpdate(
        router_output=live_export_override,
        next_step=NextStep.TOOL,
    ).to_state_update()


async def _run_router_llm(
    *,
    llm_provider: Any,
    router_messages: list[dict[str, str]],
    planner_model: str | None,
) -> RouterOutput:
    return await llm_provider.chat_structured(
        messages=router_messages,
        response_model=RouterOutput,
        model=planner_model,
        temperature=settings.LITELLM_PLANNER_TEMPERATURE,
        max_tokens=settings.OLLAMA_NUM_PREDICT_PLANNER,
    )


def _build_web_search_fallback(user_message: str) -> dict:
    query = strip_web_search_prefix(user_message)
    _dev_log("router_fallback_web_search", query=query[:120])
    fallback = RouterOutput(
        decision=RouterDecision.WEB_SEARCH,
        steps=[ToolStep(tool=SystemToolName.WEB_SEARCH.value, arguments={"query": query})],
        response_hint=_WEB_SEARCH_HINT,
        confidence=0.5,
    )
    return RouterNodeUpdate(
        router_output=fallback,
        next_step=NextStep.WEB_SEARCH,
    ).to_state_update()


def _handle_router_fallback(exc: Exception, *, user_message: str) -> dict:
    logger.warning("Router LLM failed: %s, using fallback routing", exc)
    
    # Side effect: Extract router output from malformed JSON exception
    salvaged = extract_router_output_from_exception(
        exc,
        user_message,
        web_search_pattern=WEB_SEARCH_RE,
        web_search_hint=_WEB_SEARCH_HINT,
    )
    
    # Policy: Determine which fallback mode to use (salvage, export, live_export, web_search, chat)
    has_salvage = salvaged is not None
    has_explicit_export = fallback_explicit_export_route(user_message) is not None
    has_live_export = (
        settings.ROUTER_ENABLE_DETERMINISTIC_FALLBACKS 
        and settings.ROUTER_ENABLE_LIVE_EXPORT_FALLBACK
        and fallback_live_data_export_route(user_message) is not None
    )
    is_web_search = settings.ROUTER_ENABLE_DETERMINISTIC_FALLBACKS and is_web_search_intent(user_message)
    
    fallback_mode = decide_fallback_mode(
        has_salvage=has_salvage,
        has_explicit_export=has_explicit_export,
        has_live_export=has_live_export,
        is_web_search_intent=is_web_search,
    )
    _dev_log("router_fallback_mode_selected", mode=fallback_mode)
    
    # Side effect: Execute the selected fallback mode
    if fallback_mode == "salvaged" and salvaged is not None:
        _dev_log(
            "router_fallback_salvaged",
            mode=FallbackMode.JSON_SALVAGE.value,
            decision=salvaged.decision.value,
            steps_count=len(salvaged.steps),
        )
        return RouterNodeUpdate(
            router_output=salvaged,
            next_step=next_step_from_router_decision(salvaged.decision),
        ).to_state_update()
    
    if fallback_mode == "explicit_export":
        explicit_export_fallback = fallback_explicit_export_route(user_message)
        if explicit_export_fallback is not None:
            _dev_log(
                "router_fallback_explicit_export",
                mode=FallbackMode.EXPLICIT_EXPORT.value,
                decision=explicit_export_fallback.decision.value,
                steps_count=len(explicit_export_fallback.steps),
            )
            return RouterNodeUpdate(
                router_output=explicit_export_fallback,
                next_step=next_step_from_router_decision(explicit_export_fallback.decision),
            ).to_state_update()
    
    if fallback_mode == "live_export":
        live_export_fallback = fallback_live_data_export_route(user_message)
        if live_export_fallback is not None:
            _dev_log(
                "router_fallback_live_export",
                mode=FallbackMode.LIVE_EXPORT.value,
                decision=live_export_fallback.decision.value,
                steps_count=len(live_export_fallback.steps),
            )
            return RouterNodeUpdate(
                router_output=live_export_fallback,
                next_step=NextStep.TOOL,
            ).to_state_update()
    
    if fallback_mode == "web_search":
        return _build_web_search_fallback(user_message)
    
    # Default: Chat fallback
    fallback = RouterOutput(decision=RouterDecision.CHAT, confidence=0.3)
    return RouterNodeUpdate(
        router_output=fallback,
        next_step=NextStep.CHAT,
    ).to_state_update()


async def router_node(state: dict) -> dict:
    """Classify user intent and decide the next step."""
    from app.llm import llm_provider
    from app.services.tool_catalog_service import tool_catalog_service

    context = RouterContext.from_state(state)
    user_message = context.user_message
    user_id = context.user_id
    feedback_plan = context.feedback_plan
    retrieved_tools = context.retrieved_tools
    history = context.history_messages
    _dev_log("router_start", message_preview=user_message[:120])

    # Policy decision: Should we skip expensive LLM call with fast-path logic?
    if should_skip_router_llm(feedback_plan, user_message):
        _dev_log("router_policy_skip_llm", reason="feedback_plan or empty_message")

    for fast_path in (
        _try_feedback_web_search_route(user_message, feedback_plan),
        _try_hard_route(user_message),
        _try_export_followup_route(user_message, history),
        _try_deterministic_route(user_message),
    ):
        if fast_path is not None:
            return fast_path

    integrations_block, dynamic_tools_block = await _load_router_context(user_id)
    live_data_route = _try_live_data_route(
        user_id=user_id,
        user_message=user_message,
        integrations_block=integrations_block,
        dynamic_tools_block=dynamic_tools_block,
    )
    if live_data_route is not None:
        return live_data_route

    retrieved_block = _build_retrieved_tools_block(retrieved_tools)
    planner_model = settings.LITELLM_PLANNER_MODEL or None
    planner_prompt = _build_router_prompt(
        user_message=user_message,
        feedback_plan=feedback_plan,
        tool_catalog_signatures=tool_catalog_service.planner_signatures(),
        dynamic_tools_block=dynamic_tools_block,
        integrations_block=integrations_block,
        retrieved_block=retrieved_block,
    )

    router_messages = _build_router_messages(
        planner_prompt=planner_prompt,
        history=history,
        user_message=user_message,
    )

    try:
        router_output = await _run_router_llm(
            llm_provider=llm_provider,
            router_messages=router_messages,
            planner_model=planner_model,
        )
        _dev_log(
            "router_llm",
            decision=router_output.decision.value,
            steps_count=len(router_output.steps),
            confidence=router_output.confidence,
        )
        live_export_override = _try_live_export_override(user_message, router_output)
        if live_export_override is not None:
            return live_export_override
        return RouterNodeUpdate(
            router_output=router_output,
            next_step=next_step_from_router_decision(router_output.decision),
        ).to_state_update()
    except Exception as exc:
        return _handle_router_fallback(exc, user_message=user_message)