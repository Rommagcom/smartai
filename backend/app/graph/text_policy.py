from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_SMALL_TALK_RE = re.compile(
    r"^(?:"
    r"hi|hello|hey|thanks|thank you|bye"
    r"|привет|здравствуй|здравствуйте|спасибо|пока|добрый\s+(?:день|вечер|утро)"
    r")(?:[!,.\s].*)?$",
    re.IGNORECASE,
)

_COMPLETENESS_RE = re.compile(r"COMPLETENESS:\s*(COMPLETE|INCOMPLETE)", re.IGNORECASE)


def looks_like_small_talk(text: str) -> bool:
    """Cheap heuristic for greeting/closing gratitude-style messages."""
    stripped = str(text or "").strip()
    if not stripped:
        return True
    if len(stripped) <= 24 and _SMALL_TALK_RE.match(stripped):
        return True
    return stripped.lower() in {
        "ok", "okay", "понял", "ясно", "принято", "супер", "отлично", "благодарю"
    }


def sanitize_llm_answer(text: str) -> str:
    """Remove dangerous patterns from LLM output."""
    cleaned = str(text or "")
    if not cleaned.strip():
        return "Не удалось сформировать ответ. Попробуйте уточнить запрос."

    original = cleaned
    cleaned = re.sub(r"<function_calls>[\s\S]*?</function_calls>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<invoke[\s\S]*?</invoke>", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()
    if cleaned:
        return cleaned

    logger.debug("_sanitize_llm_answer: tag strip left empty, original %d chars", len(original))
    fallback = re.sub(r"</?(?:function_calls|invoke)[^>]*>", "", original, flags=re.IGNORECASE).strip()
    return fallback or "Не удалось сформировать ответ. Попробуйте уточнить запрос."


def extract_completeness(text: str) -> bool:
    """Extract the COMPLETENESS marker from LLM response. Defaults to True."""
    match = _COMPLETENESS_RE.search(text or "")
    if match:
        return match.group(1).upper() == "COMPLETE"
    return True


def strip_completeness_marker(text: str) -> str:
    """Remove the COMPLETENESS: ... marker line from LLM output."""
    return _COMPLETENESS_RE.sub("", text or "").strip()
