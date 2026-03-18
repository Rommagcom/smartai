"""Structured output parsing with LiteLLM and Pydantic v2.

Handles extracting and validating JSON from LLM responses,
with specialized error detection and recovery.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Type, TypeVar

from pydantic import BaseModel, ValidationError

from app.core.config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class StructuredParseError(Exception):
    """Raised when structured JSON payload cannot be extracted from LLM text."""


def _is_tool_payload_schema_mismatch(exc: Exception) -> bool:
    """Detect cases where model returned a tool call object for a non-tool schema."""
    if not isinstance(exc, ValidationError):
        return False
    try:
        for item in exc.errors() or []:
            payload = item.get("input")
            if isinstance(payload, dict) and "tool" in payload:
                return True
    except Exception:
        return False
    return False


def _compact_structured_parse_error(exc: Exception) -> str:
    """Return a short, redacted parse error summary for logs."""
    if isinstance(exc, ValidationError):
        try:
            first = (exc.errors() or [{}])[0]
            code = str(first.get("type") or "validation_error")
            msg = str(first.get("msg") or "validation failed")
            return f"{code}: {msg}"
        except Exception:
            pass

    text = str(exc or "").strip()
    if not text:
        return "structured parse failed"

    # Hide verbose payload snippets often present in pydantic/litellm errors.
    text = re.sub(r"input_value\s*=\s*'[^']*'", "input_value='<redacted>'", text, flags=re.IGNORECASE)
    text = re.sub(r"input_value\s*=\s*\"[^\"]*\"", "input_value=\"<redacted>\"", text, flags=re.IGNORECASE)
    return text[:240]


def _is_expected_structured_parse_error(exc: Exception) -> bool:
    """Check if error is expected (JSON format issue, not a bug)."""
    text = str(exc or "").lower()
    if isinstance(exc, ValidationError):
        try:
            for item in exc.errors() or []:
                if str(item.get("type") or "").lower() == "json_invalid":
                    return True
        except Exception:
            pass
    return (
        "structured json payload not found" in text
        or "invalid json" in text
        or "json_invalid" in text
    )


def _log_structured_parse_failure(attempt: int, retries: int, exc: Exception) -> None:
    """Log structured parse failure with appropriate severity level."""
    configured = str(getattr(settings, "LITELLM_STRUCTURED_PARSE_LOG_LEVEL", "INFO") or "INFO").upper()
    expected = _is_expected_structured_parse_error(exc)
    compact = _compact_structured_parse_error(exc)
    message = "structured parse attempt %d/%d failed: %s"
    if configured == "DEBUG":
        logger.debug(message, attempt, retries, compact)
        return
    if configured == "WARNING":
        logger.warning(message, attempt, retries, compact)
        return
    # INFO (default): keep expected parser misses less noisy.
    if expected:
        logger.info(message, attempt, retries, compact)
    else:
        logger.warning(message, attempt, retries, compact)


def parse_structured_response(raw: str, model: Type[T]) -> T:
    """Extract and validate JSON from LLM response text.
    
    Tries multiple strategies:
    1. Direct JSON parse of full text
    2. Extract from markdown code fences
    3. Find first JSON object in text
    """
    text = raw.strip()

    # Try direct parse
    try:
        return model.model_validate_json(text)
    except (ValidationError, json.JSONDecodeError):
        pass

    # Strip markdown fences
    fenced = re.search(r"```(?:json)?\s*\n?([\s\S]*?)```", text, re.IGNORECASE)
    if fenced:
        try:
            return model.model_validate_json(fenced.group(1).strip())
        except (ValidationError, json.JSONDecodeError):
            pass

    # Find first JSON object
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        candidate = text[brace_start: brace_end + 1]
        try:
            return model.model_validate_json(candidate)
        except (ValidationError, json.JSONDecodeError):
            pass

    raise StructuredParseError(f"Structured JSON payload not found in LLM response: {text[:8000]}")
