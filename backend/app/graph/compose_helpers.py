"""Helper functions for compose node pipeline.

These are extracted from compose_node to avoid circular imports
and to keep them reusable for both old and new code paths.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel

from app.core.config import settings
from app.graph.orchestration_types import SystemToolName
from app.graph.router_recovery import (
    extract_non_json_answer_from_exception,
)
from app.graph.text_policy import (
    looks_like_incomplete_markdown_answer as _looks_like_incomplete_markdown_answer,
    sanitize_llm_answer as _sanitize_llm_answer,
)
from app.graph.tool_result_formatter import build_raw_tool_summary
from app.graph.web_fallback import (
    build_raw_web_summary,
    web_result_field,
)
from app.graph.prompt_builders import (
    build_compose_prompt,
    build_doc_ask_compose_prompt,
    build_recovered_answer_prompt,
    build_web_fallback_prompt,
)
from app.schemas.graph import ToolResult

logger = logging.getLogger(__name__)


class ReflexionComposeOutput(BaseModel):
    is_complete: bool
    answer: str = ""
    feedback_plan: str = ""


def _dev_log(event: str, **ctx: Any) -> None:
    if not settings.DEV_VERBOSE_LOGGING:
        return
    logger.info(
        f"graph node: {event}",
        extra={"context": {"component": "langgraph", "event": event, **ctx}},
    )


# ============================================================================
# Context Building Helpers
# ============================================================================


def _build_context_text(
    *,
    state_context: list[str],
    web_fetch_content: str,
    web_search_results: list[dict],
    tool_results: list[ToolResult],
) -> str:
    """Concatenate all context layers into single text block."""
    context_chunks = list(state_context)

    if web_fetch_content:
        context_chunks.append(web_fetch_content[:12000])
    elif web_search_results:
        snippets = "\n".join(
            f"- {web_result_field(r, 'title')}: {web_result_field(r, 'snippet')} ({web_result_field(r, 'url')})"
            for r in web_search_results[:5]
        )
        if snippets:
            context_chunks.append(f"Сниппеты web_search:\n{snippets}")

    if tool_results:
        sanitized_results = []
        strip_keys = {"headers", "file_base64", "base64"}
        for tool_result in tool_results:
            dumped = tool_result.model_dump()
            result = dumped.get("result")
            if isinstance(result, dict):
                dumped["result"] = {k: v for k, v in result.items() if k not in strip_keys}
            sanitized_results.append(dumped)
        context_chunks.append(
            "Результаты инструментов:\n"
            + json.dumps(sanitized_results, ensure_ascii=False, default=str)[:16000]
        )

    return "\n\n".join([chunk for chunk in context_chunks if str(chunk).strip()])


def _build_web_context(
    web_fetch_content: str, web_search_results: list[dict], *, limit: int = 8
) -> str:
    """Build web context from fetch results or search snippets."""
    web_context = web_fetch_content.strip()
    if web_context or not web_search_results:
        return web_context
    return "\n".join(
        f"- {web_result_field(r, 'title')}: {web_result_field(r, 'snippet')} ({web_result_field(r, 'url')})"
        for r in web_search_results[:limit]
    )


# ============================================================================
# Result Building Helpers
# ============================================================================


def _build_complete_result(*, answer: str, iterations: int) -> dict:
    """Build complete (final) compose result."""
    return {
        "final_answer": answer,
        "is_complete": True,
        "feedback_plan": "",
        "iterations": iterations,
        "iteration": iterations,
    }


def _build_incomplete_result(*, feedback_plan: str, iterations: int) -> dict:
    """Build incomplete (needs feedback loop) compose result."""
    return {
        "final_answer": "",
        "is_complete": False,
        "feedback_plan": feedback_plan,
        "iterations": iterations,
        "iteration": iterations,
    }


def _should_use_recovered_answer(recovered: str, *, has_web_context: bool) -> bool:
    """Check if recovered web answer is good enough."""
    recovered = _sanitize_llm_answer(recovered)
    if not recovered:
        return False
    if not has_web_context:
        return True
    if _looks_like_incomplete_markdown_answer(recovered):
        return False
    return len(recovered) >= 40 or any(mark in recovered for mark in (".", "!", "?", "+"))


# ============================================================================
# Doc-Ask Helpers
# ============================================================================


def _extract_doc_ask_result(tool_results: list[ToolResult]) -> dict | None:
    """Extract doc_ask tool result if present."""
    if not tool_results:
        return None
    for result in tool_results:
        if str(result.tool or "") == SystemToolName.DOC_ASK.value and result.success:
            if isinstance(result.result, dict):
                return result.result
    return None


def _build_doc_context(doc_items: Any) -> str:
    """Format document context chunks for LLM."""
    source_lines: list[str] = []
    if not isinstance(doc_items, list):
        return ""

    for idx, item in enumerate(doc_items[:10], start=1):
        if not isinstance(item, dict):
            continue
        source_doc = str(item.get("source_doc") or "документ").strip()
        chunk = str(item.get("chunk_text") or "").strip()
        if len(chunk) > 1200:
            chunk = chunk[:1200].rstrip() + "..."
        score = item.get("score")
        score_text = f"{float(score):.3f}" if isinstance(score, (int, float)) else "n/a"
        source_lines.append(f"{idx}) {source_doc} (score={score_text})")
        if chunk:
            source_lines.append(chunk)
    return "\n\n".join(source_lines).strip()


# ============================================================================
# Message Building Helper
# ============================================================================


def _build_compose_messages(
    *, prompt: str, history: list[dict], user_message: str
) -> list[dict[str, str]]:
    """Build message list for compose LLM call."""
    compose_messages: list[dict[str, str]] = [{"role": "system", "content": prompt}]
    compose_messages.extend(history[-settings.CONTEXT_ALWAYS_KEEP_LAST_MESSAGES:])
    compose_messages.append({"role": "user", "content": user_message})
    return compose_messages


# ============================================================================
# Truncation & Recovery Helpers
# ============================================================================


async def _recover_truncated_web_answer(
    *,
    llm_provider: Any,
    answer: str,
    user_message: str,
    web_fetch_content: str,
    web_search_results: list[dict],
    tool_results: list[ToolResult],
) -> str:
    """Recover incomplete markdown answer using web context."""
    web_context = web_fetch_content.strip()
    if not web_context and web_search_results:
        web_context = "\n".join(
            f"- {web_result_field(r, 'title')}: {web_result_field(r, 'snippet')} ({web_result_field(r, 'url')})"
            for r in web_search_results[:8]
        )

    try:
        messages, _ = build_recovered_answer_prompt(user_message, web_context[:12000])
        recovered = await llm_provider.chat(
            messages=messages,
            temperature=0.0,
            max_tokens=settings.OLLAMA_NUM_PREDICT,
        )
        recovered = _sanitize_llm_answer(recovered)
        if recovered and not _looks_like_incomplete_markdown_answer(recovered):
            return recovered
    except Exception:
        logger.debug("truncated web answer recovery failed", exc_info=True)

    return build_raw_web_summary(
        web_fetch_content=web_fetch_content,
        web_search_results=web_search_results,
        tool_results=tool_results,
        sanitize_answer=_sanitize_llm_answer,
    )


# ============================================================================
# Web Fallback Synthesis
# ============================================================================


async def _synthesize_web_fallback(
    *,
    llm_provider: Any,
    user_message: str,
    web_fetch_content: str,
    web_search_results: list[dict],
    tool_results: list[ToolResult],
    existing_answer: str,
) -> str:
    """Synthesize answer from web context when compose fails."""
    web_context = _build_web_context(web_fetch_content, web_search_results)
    try:
        messages, _ = build_web_fallback_prompt(user_message, web_context)
        fallback_answer = await llm_provider.chat(
            messages=messages,
            temperature=0.0,
            max_tokens=settings.OLLAMA_NUM_PREDICT,
        )
        return await _recover_truncated_web_answer(
            llm_provider=llm_provider,
            answer=fallback_answer,
            user_message=user_message,
            web_fetch_content=web_fetch_content,
            web_search_results=web_search_results,
            tool_results=tool_results,
        )
    except Exception:
        logger.debug("web fallback synthesis failed", exc_info=True)

    fallback_answer = existing_answer or build_raw_tool_summary(tool_results)

    return build_raw_web_summary(
        web_fetch_content=web_fetch_content,
        web_search_results=web_search_results,
        tool_results=tool_results,
        sanitize_answer=_sanitize_llm_answer,
    ) or fallback_answer


# ============================================================================
# Recovery & Finalization (Exported from compose_node)
# ============================================================================
# Note: _recover_compose_failure and _finalize_complete_compose
# stay in compose_node.py as they are complex and orchestrate other helpers.
# The pipeline imports them directly from compose_node.
