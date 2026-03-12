from __future__ import annotations

import re
from re import Pattern

from app.schemas.graph import RouterDecision, RouterOutput, ToolStep

_STRUCTURED_PARSE_MARKERS = (
    "Structured JSON payload not found in LLM response:",
    "No valid JSON found in LLM response:",
)


def extract_non_json_answer_from_exception(exc: Exception) -> str:
    """Extract raw assistant text from structured parse exceptions when possible."""
    text = str(exc or "")
    marker = next((m for m in _STRUCTURED_PARSE_MARKERS if m in text), None)
    if marker is None:
        return ""
    idx = text.find(marker)
    candidate = text[idx + len(marker):].strip()

    # Try extracting answer field from JSON-like text first.
    match = re.search(r'"answer"\s*:\s*"([\s\S]*?)"\s*,\s*"feedback_plan"', candidate)
    if match:
        extracted = match.group(1)
        try:
            extracted = bytes(extracted, "utf-8").decode("unicode_escape")
        except Exception:
            extracted = match.group(1)
        cleaned = extracted.replace('\\"', '"').strip()
        if cleaned:
            return cleaned[:12000]

    return candidate[:4000]


def extract_router_output_from_exception(
    exc: Exception,
    user_message: str,
    web_search_pattern: Pattern[str],
    web_search_hint: str,
) -> RouterOutput | None:
    """Recover a minimal RouterOutput from malformed JSON text in exception message."""
    text = str(exc or "")
    marker = next((m for m in _STRUCTURED_PARSE_MARKERS if m in text), None)
    if marker is None:
        return None
    idx = text.find(marker)

    candidate = text[idx + len(marker):].strip()
    if not candidate:
        return None

    decision_match = re.search(
        r'"decision"\s*:\s*"(tool|chat|memory|clarify|web_search)"',
        candidate,
        re.IGNORECASE,
    )

    if _is_web_search_salvage(candidate=candidate, decision_match=decision_match):
        return _build_web_search_output(
            candidate=candidate,
            user_message=user_message,
            web_search_pattern=web_search_pattern,
            web_search_hint=web_search_hint,
        )

    if not decision_match:
        return None

    decision_raw = decision_match.group(1).lower()

    if decision_raw == RouterDecision.TOOL.value:
        # Unsafe to execute arbitrary half-parsed tool calls with missing arguments.
        return None

    if decision_raw == RouterDecision.MEMORY.value:
        decision = RouterDecision.MEMORY
    elif decision_raw == RouterDecision.CLARIFY.value:
        decision = RouterDecision.CLARIFY
    else:
        decision = RouterDecision.CHAT

    return RouterOutput(decision=decision, confidence=0.4)


def _is_web_search_salvage(candidate: str, decision_match: re.Match[str] | None) -> bool:
    if decision_match and decision_match.group(1).lower() == RouterDecision.WEB_SEARCH.value:
        return True
    # Sometimes JSON is truncated before the decision field closes.
    return (not decision_match) and bool(
        re.search(r'"tool"\s*:\s*"web_search"', candidate, re.IGNORECASE)
    )


def _build_web_search_output(
    candidate: str,
    user_message: str,
    web_search_pattern: Pattern[str],
    web_search_hint: str,
) -> RouterOutput:
    query_match = re.search(r'"query"\s*:\s*"([\s\S]*?)"', candidate, re.IGNORECASE)
    query = (query_match.group(1) if query_match else "").replace('\\"', '"').strip()
    query = query or web_search_pattern.sub("", user_message).strip() or user_message
    return RouterOutput(
        decision=RouterDecision.WEB_SEARCH,
        steps=[ToolStep(tool="web_search", arguments={"query": query})],
        response_hint=web_search_hint,
        confidence=0.45,
    )
