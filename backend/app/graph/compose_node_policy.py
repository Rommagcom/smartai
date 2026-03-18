"""Pure policy for compose/refinement decisions.

This module contains logic for answer composition decisions WITHOUT side effects:
- No LLM calls
- No database access
- No HTTP requests
- No external service dependencies

All functions are pure and testable without mocks.
"""
from __future__ import annotations

from typing import Any


def should_continue_compose_iteration(
    current_iteration: int,
    max_iterations: int,
    is_complete: bool,
    feedback_plan: str,
) -> bool:
    """Pure: Decide if we should continue refinement iterations.
    
    Policy for composition loop termination.
    """
    if is_complete:
        # Already marked complete by LLM
        return False
    
    if current_iteration >= max_iterations:
        # Hit max iterations limit
        return False
    
    if not feedback_plan or not feedback_plan.strip():
        # No feedback to apply
        return False
    
    return True


def should_use_fallback_compose(
    answer: str,
    tool_results: list[dict],
    web_context: str,
) -> bool:
    """Pure: Decide if we need fallback composition (no LLM).
    
    Cases: LLM unavailable, structured parse fails, etc.
    """
    # If answer already exists and we have context, can use fallback
    if answer and (tool_results or web_context):
        return True
    
    return False


def can_extract_answer_from_web_context(web_context: str) -> bool:
    """Pure: Check if we can build answer from web data without LLM.
    
    For web-search cases where we have good snippets.
    """
    if not web_context:
        return False
    
    # Must have some actual content
    return len(str(web_context).strip()) > 50


def should_sanitize_false_export_claims(
    final_answer: str,
    tool_calls: list[dict],
) -> bool:
    """Pure: Decide if answer contains false export success claims.
    
    Detects: "Created PDF report" but pdf_create tool failed/not called.
    """
    successful_exports = [
        c for c in tool_calls
        if str(c.get("tool") or "").lower() in {"pdf_create", "excel_create"}
        and bool(c.get("success"))
    ]
    
    if successful_exports:
        # Real exports happened, claims are valid
        return False
    
    # Check if answer claims export
    lowered = str(final_answer or "").lower()
    
    export_claims = [
        "создал pdf", "создал excel", "сгенерировал pdf",
        "generated pdf", "created excel", "exported",
        "файл готов", "документ готов", "pdf saved",
    ]
    
    return any(claim in lowered for claim in export_claims)


def extract_compose_input_safely(
    state: dict,
) -> dict:
    """Pure: Extract compose input from state, with safe defaults.
    
    Factory method - builds clean input dict without side effects.
    """
    return {
        "user_message": str(state.get("user_message") or "").strip(),
        "messages": state.get("messages") or [],
        "tool_results": state.get("tool_results") or [],
        "web_fetch_content": str(state.get("web_fetch_content") or "").strip(),
        "web_search_results": state.get("web_search_results") or [],
        "final_answer": str(state.get("final_answer") or "").strip(),
        "iterations": int(state.get("iterations") or 0),
        "max_iterations": int(state.get("max_iterations") or 5),
        "iteration": int(state.get("iteration") or 0),
    }
