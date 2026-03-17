"""Pure policy for output post-processing decisions.

This module contains logic for output decisions WITHOUT side effects:
- No LLM calls
- No database access  
- No HTTP requests
- No external service dependencies

All functions are pure and testable without mocks.
"""
from __future__ import annotations

from typing import Any


def should_attempt_direct_route(
    user_id: Any,
    user_message: str,
    all_calls: list[dict],
) -> bool:
    """Pure: Decide if we should attempt direct (deterministic) tool routing as fallback.
    
    This decision should work regardless of whether we have DB access or services.
    """
    # Already have successful tool calls
    if all_calls:
        return False
    
    # No user context
    if not user_id:
        return False
    
    # No message to parse
    if not user_message:
        return False
    
    return True


def should_reenqueue_artifact_export(
    export_status: str | None,
    final_answer: str,
) -> bool:
    """Pure: Decide if we should reenqueue export when it failed/delayed.
    
    Pure logic that doesn't depend on DB or services.
    """
    # No explicit export status = not relevant
    if not export_status:
        return False
    
    # Status indicates we should try again
    if export_status not in {"queued", "delayed"}:
        return False
    
    # Make sure we have the main content
    if not final_answer or not final_answer.strip():
        return False
    
    return True


def can_recover_from_guardrail_block(
    guardrail_verdict: str,
    has_fallback_content: bool,
) -> bool:
    """Pure: Decide if we can recover when guardrail blocks answer.
    
    Policy: can we provide partial answer or must block?
    """
    # Some blocks are hard failures
    if guardrail_verdict in {"security_threat", "abuse"}:
        return False
    
    # For other cases, recovery depends on having fallback
    return has_fallback_content


def should_apply_export_success_claim_sanitization(
    final_answer: str,
    tool_calls: list[dict],
) -> bool:
    """Pure: Decide if answer might contain false export success claims.
    
    Detects cases where LLM claims export happened but no tool succeeded.
    """
    # No tools called, but answer claims export = false claim
    successful_exports = [
        c for c in tool_calls
        if str(c.get("tool") or "").lower() in {"pdf_create", "excel_create"}
        and bool(c.get("success"))
    ]
    
    if successful_exports:
        # Real exports happened, don't sanitize
        return False
    
    # Check if answer claims export
    return _contains_export_success_claim(final_answer)


def _contains_export_success_claim(text: str) -> bool:
    """Check if text claims successful document export."""
    if not text:
        return False
    
    lowered = str(text or "").lower()
    
    # Claims of PDF/Excel generation
    has_export_subject = any(
        subject in lowered
        for subject in [
            "pdf", "пдф", "excel", "xlsx", "документ",
            "table", "таблица", "файл", "file"
        ]
    )
    
    # Claims of completion
    has_completion = any(
        phrase in lowered
        for phrase in [
            "создал", "сформировал", "готовый", "готов", "сохранил",
            "сгенерировал", "сделал", "exported", "created", "ready"
        ]
    )
    
    return has_export_subject and has_completion
