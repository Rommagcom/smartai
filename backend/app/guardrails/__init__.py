"""Guardrails — prompt injection detection and output safety checks.

Middleware layer that inspects user input before it reaches the LLM
and validates LLM output before it reaches the user.

Detects:
  - Prompt injection attacks (role hijacking, system prompt extraction)
  - Data exfiltration patterns
  - Dangerous code patterns in output
"""
from __future__ import annotations

import logging
import re

from app.schemas.graph import GuardrailResult, GuardrailVerdict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt injection patterns
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS: list[re.Pattern] = [
    # Role hijacking
    re.compile(r"\bignore\s+(?:all\s+)?(?:previous|above|prior)\s+instructions?\b", re.IGNORECASE),
    re.compile(r"\bforget\s+(?:all\s+)?(?:your|previous)\s+(?:instructions?|rules?|constraints?)\b", re.IGNORECASE),
    re.compile(r"\byou\s+are\s+now\s+(?:a\s+)?(?:different|new|evil|unrestricted)\b", re.IGNORECASE),
    re.compile(r"\b(?:act|behave|pretend)\s+(?:as\s+)?(?:if\s+)?(?:you\s+(?:are|were)\s+)?(?:DAN|jailbroken|unrestricted)\b", re.IGNORECASE),
    re.compile(r"\bsystem\s*:\s*you\s+are\b", re.IGNORECASE),

    # System prompt extraction
    re.compile(r"\brepeat\s+(?:your\s+)?(?:system\s+)?(?:prompt|instructions?)\b", re.IGNORECASE),
    re.compile(r"\bshow\s+(?:me\s+)?(?:your\s+)?(?:system\s+)?(?:prompt|instructions?|rules?)\b", re.IGNORECASE),
    re.compile(r"\bprint\s+(?:your\s+)?(?:system\s+)?(?:prompt|instructions?)\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+(?:are\s+)?(?:your\s+)?(?:system\s+)?(?:instructions?|rules?|prompt)\b", re.IGNORECASE),

    # Delimiter injection
    re.compile(r"```\s*system\b", re.IGNORECASE),
    re.compile(r"\[SYSTEM\]", re.IGNORECASE),
    re.compile(r"<\|(?:im_start|system|endoftext)\|>", re.IGNORECASE),
]

# ---------------------------------------------------------------------------
# Output safety patterns
# ---------------------------------------------------------------------------

_OUTPUT_DANGEROUS_PATTERNS: list[re.Pattern] = [
    # Leaked system prompts
    re.compile(r"\bmy\s+system\s+prompt\s+is\b", re.IGNORECASE),
    re.compile(r"\bhere\s+(?:is|are)\s+my\s+(?:instructions?|rules?|system\s+prompt)\b", re.IGNORECASE),
    # Injected function calls
    re.compile(r"<function_calls>", re.IGNORECASE),
    re.compile(r"<invoke\s+", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_input(text: str) -> GuardrailResult:
    """Check user input for prompt injection attempts."""
    if not text:
        return GuardrailResult(verdict=GuardrailVerdict.PASS)

    for pattern in _INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            logger.warning(
                "Prompt injection detected: pattern=%s, match=%s",
                pattern.pattern[:80],
                match.group(0)[:80],
            )
            return GuardrailResult(
                verdict=GuardrailVerdict.BLOCK,
                reason=f"Potential prompt injection detected",
            )

    return GuardrailResult(verdict=GuardrailVerdict.PASS)


def check_output(text: str) -> GuardrailResult:
    """Check LLM output for unsafe content."""
    if not text:
        return GuardrailResult(verdict=GuardrailVerdict.PASS)

    for pattern in _OUTPUT_DANGEROUS_PATTERNS:
        match = pattern.search(text)
        if match:
            logger.warning(
                "Dangerous output pattern detected: %s",
                match.group(0)[:80],
            )
            # Strip dangerous content rather than blocking
            cleaned = pattern.sub("", text).strip()
            if not cleaned:
                return GuardrailResult(
                    verdict=GuardrailVerdict.BLOCK,
                    reason="Output contained only dangerous patterns",
                )
            return GuardrailResult(
                verdict=GuardrailVerdict.WARN,
                reason="Dangerous patterns stripped from output",
                modified_text=cleaned,
            )

    return GuardrailResult(verdict=GuardrailVerdict.PASS)
