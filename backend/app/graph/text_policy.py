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
_TOOL_BLOCK_RE = re.compile(
    r"<(?:cron_add|pdf_create|excel_create|doc_ask|web_search|"
    r"memory_add|memory_list|memory_search|memory_delete|memory_delete_all|"
    r"function_calls|invoke)[\s\S]*?>"
    r"[\s\S]*?"
    r"</(?:cron_add|pdf_create|excel_create|doc_ask|web_search|"
    r"memory_add|memory_list|memory_search|memory_delete|memory_delete_all|"
    r"function_calls|invoke)>",
    re.IGNORECASE,
)
_GENERIC_COMMAND_BLOCK_RE = re.compile(
    r"<([a-z][a-z0-9]*_[a-z0-9_:-]*)[^>]*>[\s\S]*?</\1>",
    re.IGNORECASE,
)
_GENERIC_COMMAND_TAG_RE = re.compile(
    r"</?[a-z][a-z0-9]*_[a-z0-9_:-]*[^>]*>",
    re.IGNORECASE,
)
_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```", re.IGNORECASE)
_TABLE_LINE_RE = re.compile(r"^\s*\|.*$")


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
    # Remove explicit tool command payloads that must never be shown to users.
    cleaned = _TOOL_BLOCK_RE.sub("", cleaned)
    cleaned = _GENERIC_COMMAND_BLOCK_RE.sub("", cleaned)
    cleaned = re.sub(r"<function_calls>[\s\S]*?</function_calls>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<invoke[\s\S]*?</invoke>", "", cleaned, flags=re.IGNORECASE)
    cleaned = _GENERIC_COMMAND_TAG_RE.sub("", cleaned)
    # Strip fenced blocks too: fallback paths may return raw command snippets in markdown.
    cleaned = _CODE_FENCE_RE.sub("", cleaned)
    cleaned = cleaned.strip()
    if cleaned:
        return cleaned

    logger.debug("_sanitize_llm_answer: tag strip left empty, original %d chars", len(original))
    fallback = _TOOL_BLOCK_RE.sub("", original)
    fallback = _GENERIC_COMMAND_BLOCK_RE.sub("", fallback)
    fallback = re.sub(r"</?(?:function_calls|invoke)[^>]*>", "", fallback, flags=re.IGNORECASE)
    fallback = _GENERIC_COMMAND_TAG_RE.sub("", fallback)
    fallback = _CODE_FENCE_RE.sub("", fallback)
    fallback = fallback.strip()
    return fallback or "Не удалось сформировать ответ. Попробуйте уточнить запрос."


def looks_like_incomplete_markdown_answer(text: str) -> bool:
    """Heuristic for truncated markdown that commonly appears in web answers."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return False

    if cleaned.count("```") % 2 == 1:
        return True

    lines = [line.rstrip() for line in cleaned.splitlines() if line.strip()]
    if len(lines) < 2:
        return False

    table_lines = [line.strip() for line in lines if _TABLE_LINE_RE.match(line)]
    if len(table_lines) < 2:
        return False

    last_line = lines[-1].strip()
    if not last_line.startswith("|"):
        return False

    if not last_line.endswith("|"):
        return True

    previous_table_line = next(
        (line for line in reversed(lines[:-1]) if line.strip().startswith("|")),
        "",
    ).strip()
    if previous_table_line and last_line.count("|") < previous_table_line.count("|"):
        return True

    return False


def extract_completeness(text: str) -> bool:
    """Extract the COMPLETENESS marker from LLM response. Defaults to True."""
    match = _COMPLETENESS_RE.search(text or "")
    if match:
        return match.group(1).upper() == "COMPLETE"
    return True


def strip_completeness_marker(text: str) -> str:
    """Remove the COMPLETENESS: ... marker line from LLM output."""
    return _COMPLETENESS_RE.sub("", text or "").strip()
