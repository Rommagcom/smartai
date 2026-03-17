"""Helper functions for output node pipeline.

Extracted from output_node to enable reuse and avoid circular imports.
"""
import asyncio
import logging
from typing import Any

from app.core.config import settings
from app.graph.artifact_utils import extract_artifacts
from app.graph.compose_node import _recover_truncated_web_answer
from app.graph.output_policy import (
    has_successful_export_call,
    requested_export_kind,
    sanitize_false_attachment_claims,
    should_reenqueue_export,
)
from app.graph.output_postprocess import (
    apply_direct_route_fallback,
    apply_inline_cron_bridge,
    apply_inline_integration_bridge,
    apply_output_guardrail,
    enqueue_export_if_needed,
)
from app.graph.post_output_tasks import extract_facts_to_ltm
from app.graph.text_policy import (
    sanitize_llm_answer as _sanitize_llm_answer,
)
from app.graph.tool_result_formatter import build_raw_tool_summary
from app.graph.web_fallback import build_raw_web_summary

logger = logging.getLogger(__name__)


def _dev_log(event: str, **ctx: Any) -> None:
    """Dev mode logging for output events."""
    if not settings.DEV_VERBOSE_LOGGING:
        return
    logger.info(
        f"graph node: {event}",
        extra={"context": {"component": "langgraph", "event": event, **ctx}},
    )


async def apply_output_bridges(
    *,
    user_id: Any,
    user_message: str,
    final_answer: str,
    all_calls: list[dict],
    all_artifacts: list[dict],
) -> tuple[str, list[dict], list[dict]]:
    """Apply sequential output bridges: direct route, cron, integration.
    
    Each bridge can modify answer and call/artifact logs.
    
    Args:
        user_id: User identifier
        user_message: Original user message
        final_answer: Current answer text
        all_calls: Tool calls log
        all_artifacts: Extracted artifacts
    
    Returns:
        Tuple of (final_answer, all_calls, all_artifacts) after bridges applied
    """
    final_answer, all_calls, all_artifacts = await apply_direct_route_fallback(
        user_id=user_id,
        user_message=user_message,
        final_answer=final_answer,
        all_calls=all_calls,
        all_artifacts=all_artifacts,
    )
    final_answer, all_calls, all_artifacts = await apply_inline_cron_bridge(
        user_id=user_id,
        user_message=user_message,
        final_answer=final_answer,
        all_calls=all_calls,
        all_artifacts=all_artifacts,
    )
    return await apply_inline_integration_bridge(
        user_id=user_id,
        user_message=user_message,
        final_answer=final_answer,
        all_calls=all_calls,
        all_artifacts=all_artifacts,
    )


def append_export_status(
    final_answer: str,
    *,
    extra_calls: list[dict],
    export_kind: str | None,
) -> str:
    """Append export status message to final answer if export was queued.
    
    Args:
        final_answer: Current answer text
        extra_calls: New tool calls from export operation
        export_kind: Type of export ('pdf', 'excel', None)
    
    Returns:
        Final answer with appended status, or unchanged if no export
    """
    if not (extra_calls and export_kind and has_successful_export_call(extra_calls, export_kind)):
        return final_answer
    status_text = "PDF" if export_kind == "pdf" else "Excel"
    return (
        f"{final_answer}\n\n"
        f"{status_text} поставлен в очередь и будет отправлен отдельным сообщением."
    )


async def recover_web_answer_if_needed(
    *,
    llm_provider: Any,
    final_answer: str,
    user_message: str,
    web_fetch_content: str,
    web_search_results: list[dict],
    tool_results: list[Any],
) -> str:
    """Recover truncated web result if answer appears incomplete.
    
    Delegates to compose_node's recovery logic. Only runs if web context exists.
    
    Args:
        llm_provider: LLM provider instance
        final_answer: Answer to check and possibly recover
        user_message: Original user message for context
        web_fetch_content: Web fetch result content
        web_search_results: Web search results
        tool_results: Tool results from execution
    
    Returns:
        Recovered answer or original if no recovery needed
    """
    if not (web_fetch_content or web_search_results):
        return final_answer
    return await _recover_truncated_web_answer(
        llm_provider=llm_provider,
        answer=final_answer,
        user_message=user_message,
        web_fetch_content=web_fetch_content,
        web_search_results=web_search_results,
        tool_results=tool_results,
    )


async def append_memory_and_schedule_ltm(
    *,
    user_id: Any,
    user_message: str,
    final_answer: str,
) -> None:
    """Append to STM and schedule LTM extraction asynchronously.
    
    Both operations are optional (fail gracefully). LTM extraction runs in background.
    
    Args:
        user_id: User identifier
        user_message: User's message
        final_answer: Final answer to store
    """
    from app.memory import memory_manager

    if not (user_id and final_answer):
        return

    # Synchronous STM append with error handling
    try:
        await memory_manager.append_stm(user_id, user_message, final_answer)
    except Exception:
        logger.debug("STM append failed", exc_info=True)

    # Async LTM extraction in background
    try:
        ltm_task = asyncio.create_task(extract_facts_to_ltm(user_id, user_message, final_answer))
    except Exception:
        logger.debug("LTM extraction scheduling failed", exc_info=True)
    else:
        _ = ltm_task


def determine_export_reenqueue(
    *,
    export_kind: str | None,
    existing_calls: list[dict],
    existing_artifacts: list[dict],
) -> bool:
    """Determine if export should be requeued for failed previous attempt.
    
    Args:
        export_kind: Type of export requested ('pdf', 'excel', None)
        existing_calls: Existing tool calls log
        existing_artifacts: Existing artifacts log
    
    Returns:
        True if export should be requeued, False otherwise
    """
    return bool(settings.OUTPUT_ENABLE_AUTO_EXPORT_REENQUEUE) and should_reenqueue_export(
        export_kind=export_kind,
        existing_calls=existing_calls,
        existing_artifacts=existing_artifacts,
    )


def sanitize_answer_final(final_answer: str) -> tuple[str, bool]:
    """Apply final answer sanitization (guardrail + LLM cleanup).
    
    Two-step process:
    1. apply_output_guardrail: Check for prohibited content
    2. _sanitize_llm_answer: Clean up LLM output formatting
    
    Args:
        final_answer: Answer to sanitize
    
    Returns:
        Tuple of (sanitized_answer, should_log_debug)
    """
    # Step 1: Guardrail check
    final_answer, guardrail_result = apply_output_guardrail(final_answer)
    
    # Step 2: LLM output cleanup
    sanitized = _sanitize_llm_answer(final_answer)
    
    # Log if sanitization changed content
    should_log = sanitized != final_answer
    if should_log:
        _dev_log(
            "output_final_answer_sanitized",
            before_len=len(str(final_answer or "")),
            after_len=len(str(sanitized or "")),
        )
    
    return sanitized, guardrail_result
