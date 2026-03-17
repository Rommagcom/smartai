"""Pure policy for router decision-making.

This module contains logic for making routing decisions WITHOUT side effects:
- No LLM calls
- No database access
- No HTTP requests
- No external service dependencies

All functions are pure and can be tested without mocks.
"""
from __future__ import annotations

import re
from typing import Any

from app.schemas.graph import RouterDecision, RouterOutput


def should_skip_router_llm(
    feedback_plan: str,
    user_message: str
) -> bool:
    """Pure: Determine if we can skip expensive LLM call with fast-path logic.
    
    Returns True if we should try deterministic/fallback routing first.
    """
    # If user explicitly requested refinement, skip LLM and use feedback
    if feedback_plan:
        return True
    
    # No message = skip
    if not user_message:
        return True
    
    return False


def decide_fallback_mode(
    has_salvage: bool,
    has_explicit_export: bool,
    has_live_export: bool,
    is_web_search_intent: bool,
) -> str | None:
    """Pure: Determine which fallback mode to use when router LLM fails.
    
    Returns mode name (salvaged, explicit_export, live_export, web_search, chat).
    Tries modes in order of confidence.
    """
    if has_salvage:
        return "salvaged"
    if has_explicit_export:
        return "explicit_export"
    if has_live_export:
        return "live_export"
    if is_web_search_intent:
        return "web_search"
    return "chat"


def extract_router_decision_intent(exc_text: str) -> RouterDecision | None:
    """Pure: Extract router decision from malformed JSON exception text.
    
    Pure parsing logic - no I/O, no external deps.
    """
    decision_match = re.search(
        r'"decision"\s*:\s*"(tool|chat|memory|clarify|web_search)"',
        exc_text,
        re.IGNORECASE,
    )
    
    if not decision_match:
        return None
    
    decision_raw = decision_match.group(1).lower()
    
    try:
        return RouterDecision(decision_raw)
    except ValueError:
        return None


def should_attempt_live_data_web_search(
    user_id: Any,
    integrations_block: str,
    dynamic_tools_block: str,
    is_live_data_query: bool,
) -> bool:
    """Pure: Decide if we should use live-data fast-path (web_search).
    
    No side effects - just boolean policy decision.
    """
    # Live-data requires available user ID and empty tool blocks
    if not user_id:
        return False
    
    if integrations_block.strip():
        return False
    
    if dynamic_tools_block.strip():
        return False
    
    return is_live_data_query


def build_router_decision_from_salvage(
    extracted_decision: RouterDecision,
    has_tool_call: bool,
) -> RouterOutput | None:
    """Pure: Build a minimal RouterOutput from salvaged decision.
    
    Decides what to do with partially-extracted router data.
    No side effects.
    """
    if extracted_decision == RouterDecision.TOOL:
        # Unsafe to execute arbitrary truncated tool calls
        if not has_tool_call:
            return None
        # Tool salvage should be done with separate logic (PDF/Excel only)
        return None
    
    # For MEMORY and CLARIFY, treat as CHAT
    if extracted_decision == RouterDecision.MEMORY:
        return RouterOutput(decision=RouterDecision.MEMORY, confidence=0.4)
    
    if extracted_decision == RouterDecision.CLARIFY:
        return RouterOutput(decision=RouterDecision.CLARIFY, confidence=0.4)
    
    return RouterOutput(decision=RouterDecision.CHAT, confidence=0.4)


def should_apply_live_export_override(
    router_decision: RouterDecision,
    is_live_data_query: bool,
) -> bool:
    """Pure: Determine if we should override with live-export fast-path.
    
    Used after LLM has decided, but we can detect better path.
    """
    # Only override tool decisions
    if router_decision != RouterDecision.TOOL:
        return False
    
    # Only when user asks for live data
    if not is_live_data_query:
        return False
    
    return True


# Internal patterns for policy decisions
_LIVE_DATA_KEYWORDS = re.compile(
    r"\b(?:погод|weather|прогноз|курс\s+валют|валют|usd|eur|kzt|новост|цена|стоимост|сегодня|актуальн)\b",
    re.IGNORECASE,
)

_EXPORT_KEYWORDS = re.compile(
    r"\b(?:сделай|создай|сформируй|сгенерируй|выгрузи|экспорт|сохрани|оформи|отправ|пришли|generate|create|export|attach)\b",
    re.IGNORECASE,
)
