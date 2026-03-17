from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.core.config import settings
from app.graph.compose_node import _recover_truncated_web_answer
from app.graph.node_helpers import (
    _sanitize_llm_answer,
    apply_direct_route_fallback,
    apply_inline_cron_bridge,
    apply_inline_integration_bridge,
    apply_output_guardrail,
    enqueue_export_if_needed,
    extract_artifacts,
    extract_facts_to_ltm,
    has_successful_export_call,
    requested_export_kind,
    sanitize_false_attachment_claims,
    should_reenqueue_export,
)

logger = logging.getLogger(__name__)


def _dev_log(event: str, **ctx: Any) -> None:
    if not settings.DEV_VERBOSE_LOGGING:
        return
    logger.info(
        f"graph node: {event}",
        extra={"context": {"component": "langgraph", "event": event, **ctx}},
    )


async def _apply_output_bridges(
    *,
    user_id: Any,
    user_message: str,
    final_answer: str,
    all_calls: list[dict],
    all_artifacts: list[dict],
) -> tuple[str, list[dict], list[dict]]:
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


def _append_export_status(final_answer: str, *, extra_calls: list[dict], export_kind: str | None) -> str:
    if not (extra_calls and export_kind and has_successful_export_call(extra_calls, export_kind)):
        return final_answer
    status_text = "PDF" if export_kind == "pdf" else "Excel"
    return (
        f"{final_answer}\n\n"
        f"{status_text} поставлен в очередь и будет отправлен отдельным сообщением."
    )


async def _recover_web_answer_if_needed(
    *,
    llm_provider: Any,
    final_answer: str,
    user_message: str,
    web_fetch_content: str,
    web_search_results: list[dict],
    tool_results: list[Any],
) -> str:
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


async def _append_memory_and_schedule_ltm(*, user_id: Any, user_message: str, final_answer: str) -> None:
    from app.memory import memory_manager

    if not (user_id and final_answer):
        return

    try:
        await memory_manager.append_stm(user_id, user_message, final_answer)
    except Exception:
        logger.debug("STM append failed", exc_info=True)

    try:
        ltm_task = asyncio.create_task(extract_facts_to_ltm(user_id, user_message, final_answer))
    except Exception:
        logger.debug("LTM extraction scheduling failed", exc_info=True)
    else:
        _ = ltm_task


async def output_node(state: dict) -> dict:
    """Final output processing: guardrail check + STM append."""
    from app.llm import llm_provider

    final_answer = state.get("final_answer", "")
    user_message = state.get("user_message", "")
    user_id = state.get("user_id")
    existing_calls = state.get("tool_calls_log") or []
    existing_artifacts = state.get("artifacts") or []

    final_answer, result = apply_output_guardrail(final_answer)
    web_fetch_content = state.get("web_fetch_content") or ""
    web_search_results = state.get("web_search_results") or []
    tool_results = state.get("tool_results") or []

    export_kind = requested_export_kind(user_message)
    should_reenqueue = bool(settings.OUTPUT_ENABLE_AUTO_EXPORT_REENQUEUE) and should_reenqueue_export(
        export_kind=export_kind,
        existing_calls=existing_calls,
        existing_artifacts=existing_artifacts,
    )
    extra_calls = await enqueue_export_if_needed(
        user_id=user_id,
        final_answer=final_answer,
        export_kind=export_kind,
        should_reenqueue=should_reenqueue,
    )

    all_calls = [*existing_calls, *extra_calls]
    all_artifacts = [*existing_artifacts, *extract_artifacts(extra_calls)]
    final_answer, all_calls, all_artifacts = await _apply_output_bridges(
        user_id=user_id,
        user_message=user_message,
        final_answer=final_answer,
        all_calls=all_calls,
        all_artifacts=all_artifacts,
    )

    final_answer = _append_export_status(
        final_answer,
        extra_calls=extra_calls,
        export_kind=export_kind,
    )

    final_answer = sanitize_false_attachment_claims(
        answer=final_answer,
        tool_calls=all_calls,
        artifacts=all_artifacts,
    )

    final_answer = await _recover_web_answer_if_needed(
        llm_provider=llm_provider,
        final_answer=final_answer,
        user_message=user_message,
        web_fetch_content=web_fetch_content,
        web_search_results=web_search_results,
        tool_results=tool_results,
    )

    sanitized_final = _sanitize_llm_answer(final_answer)
    if sanitized_final != final_answer:
        _dev_log(
            "output_final_answer_sanitized",
            before_len=len(str(final_answer or "")),
            after_len=len(str(sanitized_final or "")),
        )
    final_answer = sanitized_final

    await _append_memory_and_schedule_ltm(
        user_id=user_id,
        user_message=user_message,
        final_answer=final_answer,
    )

    return {
        "final_answer": final_answer,
        "output_guardrail": result,
        "tool_calls_log": all_calls,
        "artifacts": all_artifacts,
    }