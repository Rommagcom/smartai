from __future__ import annotations

import re

from app.graph.output_policy import requested_export_kind
from app.schemas.graph import RouterDecision, RouterOutput, ToolStep

# Regex for deterministic web search detection — fast path, no LLM needed
WEB_SEARCH_RE = re.compile(
    r"\b(?:"
    r"найди\s+в\s+(?:интернет|сет[ий]|гугл|google)"
    r"|поищи\s+в\s+(?:интернет|сет[ий]|гугл|google)"
    r"|загугли|погугли"
    r"|поищи\s+в\s+сети"
    r"|найди\s+(?:мне\s+)?(?:в\s+)?(?:интернет|онлайн)"
    r"|search\s+(?:the\s+)?(?:web|internet|online)"
    r"|web\s*search"
    r"|поиск\s+в\s+интернет"
    r"|ищи\s+в\s+(?:интернет|сет[ий])"
    r"|найди\s+(?:в\s+)?инете"
    r"|поищи\s+(?:в\s+)?инете"
    r"|найди\s+информацию"
    r"|поищи\s+информацию"
    r")\b",
    re.IGNORECASE,
)

_LIVE_DATA_RE = re.compile(
    r"\b(?:погод|weather|прогноз|курс\s+валют|валют|usd|eur|kzt|новост|цена|стоимост|сегодня|актуальн)\b",
    re.IGNORECASE,
)

_EXPORT_FOLLOWUP_RE = re.compile(
    r"\b(?:да|yes|sure|ok|okay|ага|угу|нужен|нужно|хочу|want|need|готов(?:ый|ую|ое)?|"
    r"документ|document|pdf|пдф|excel|xlsx|файл|file|сгенерируй|generate|сформируй|create|export)\b",
    re.IGNORECASE,
)
_FOLLOWUP_CONFIRMATION_RE = re.compile(
    r"^(?:"
    r"да|ага|угу|yes|ok(?:ay)?|sure"
    r"|сохрани(?:\s+в\s+(?:pdf|excel))?"
    r"|сделай(?:\s+в\s+(?:pdf|excel))?"
    r"|в\s+(?:pdf|excel)"
    r"|пришли(?:\s+файл)?|отправь(?:\s+файл)?"
    r"|pdf|excel|xlsx|пдф"
    r")$",
    re.IGNORECASE,
)
_EXPORT_OFFER_SENTENCE_RE = re.compile(
    r"(?:если\s+нужен|если\s+нужно|if\s+you\s+need|if\s+needed)[^.!?\n]*(?:pdf|пдф|excel|xlsx|документ|document|file|файл)[^.!?\n]*(?:[.!?]|$)",
    re.IGNORECASE,
)
_ASSISTANT_STATUS_ONLY_RE = re.compile(
    r"(?:"
    r"задача\s+поставлен[ао]?\s+в\s+очеред"
    r"|документ\s+[^\n.!?]{0,120}\s+в\s+процесс[еа]\s+создани"
    r"|файл\s+[^\n.!?]{0,120}\s+в\s+процесс[еа]\s+создани"
    r"|поставлен\s+в\s+очеред"
    r"|будет\s+отправлен\s+отдельным\s+сообщени"
    r")",
    re.IGNORECASE,
)


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clean_export_source_text(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    cleaned = _EXPORT_OFFER_SENTENCE_RE.sub("", raw)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _is_assistant_status_only_text(text: str) -> bool:
    normalized = _normalize_space(text)
    if not normalized:
        return True
    # Guard against exporting service/status-only responses instead of real content.
    return bool(_ASSISTANT_STATUS_ONLY_RE.search(normalized))


def _extract_last_assistant_message(history: list[dict]) -> str:
    for item in reversed(history or []):
        if str(item.get("role") or "").lower() != "assistant":
            continue
        content = _normalize_space(str(item.get("content") or ""))
        if content:
            return str(item.get("content") or "")
    return ""


def _is_export_followup_intent(user_message: str) -> bool:
    text = _normalize_space(user_message)
    lowered = text.lower()
    if not lowered:
        return False
    # Follow-up must be a short confirmation, not a full new task.
    if len(text) > 80:
        return False
    if len(text.split()) > 8:
        return False
    if _FOLLOWUP_CONFIRMATION_RE.match(lowered):
        return True
    if requested_export_kind(text) in {"pdf", "excel"}:
        # Explicit export kind in a short phrase can still be follow-up.
        return len(text.split()) <= 6
    return bool(_EXPORT_FOLLOWUP_RE.search(lowered))


def is_web_search_intent(user_message: str) -> bool:
    """Check if user message clearly asks for a web search."""
    return bool(WEB_SEARCH_RE.search(user_message or ""))


def strip_web_search_prefix(user_message: str) -> str:
    query = WEB_SEARCH_RE.sub("", str(user_message or "")).strip()
    return query or str(user_message or "").strip()


def feedback_requires_web_search(feedback_plan: str) -> bool:
    text = str(feedback_plan or "").lower()
    markers = (
        "web_search",
        "интернет",
        "в сети",
        "в интернете",
        "найти",
        "поиск",
        "google",
        "гугл",
        "загугл",
    )
    return any(marker in text for marker in markers)


def feedback_to_search_query(feedback_plan: str, fallback_query: str) -> str:
    plan = str(feedback_plan or "").strip()
    if not plan:
        return str(fallback_query or "").strip()

    normalized = re.sub(r"^[\-\d\)\.\s]+", "", plan)
    return normalized[:500] if normalized else str(fallback_query or "").strip()


def deterministic_route(user_message: str) -> RouterOutput | None:
    """Pattern-match deterministic tool routes without LLM."""
    from app.services.chat_service import ChatService

    steps = ChatService._deterministic_tool_steps(user_message)
    if steps:
        tool_steps = [
            ToolStep(tool=step["tool"], arguments=step.get("arguments", {}))
            for step in steps
        ]
        return RouterOutput(
            decision=RouterDecision.TOOL,
            steps=tool_steps,
            confidence=0.95,
        )

    if is_web_search_intent(user_message):
        query = strip_web_search_prefix(user_message)
        return RouterOutput(
            decision=RouterDecision.WEB_SEARCH,
            steps=[ToolStep(tool="web_search", arguments={"query": query})],
            response_hint="Выполни поиск в интернете и представь результаты",
            confidence=0.95,
        )

    return None


def followup_export_route(user_message: str, history: list[dict]) -> RouterOutput | None:
    """Route short export confirmations to create doc from last assistant answer."""
    if not _is_export_followup_intent(user_message):
        return None

    last_assistant = _extract_last_assistant_message(history)
    if not last_assistant:
        return None

    content = _clean_export_source_text(last_assistant)
    if _is_assistant_status_only_text(content):
        return None
    if len(content) < 12:
        return None

    export_kind = requested_export_kind(user_message)
    if export_kind not in {"pdf", "excel"}:
        export_kind = "excel" if re.search(r"\b(?:excel|xlsx|таблиц)\b", user_message, re.IGNORECASE) else "pdf"

    if export_kind == "excel":
        tool = "excel_create"
        filename = "generated-document.xlsx"
    else:
        tool = "pdf_create"
        filename = "generated-document.pdf"

    return RouterOutput(
        decision=RouterDecision.TOOL,
        steps=[
            ToolStep(
                tool=tool,
                arguments={
                    "title": "Документ",
                    "filename": filename,
                    "content": content,
                },
            )
        ],
        response_hint="Сформируй документ на основе предыдущего ответа ассистента",
        confidence=0.93,
    )


def fallback_live_data_export_route(user_message: str) -> RouterOutput | None:
    """Build deterministic web_search -> export chain when router LLM fails."""
    export_kind = requested_export_kind(user_message)
    lowered = str(user_message or "").strip().lower()
    if not export_kind:
        has_export_verb = bool(
            re.search(
                r"\b(?:сделай|создай|сформируй|сгенерируй|выгрузи|экспорт|сохрани|оформи|отправ|пришли|generate|create|export|attach)\b",
                lowered,
            )
        )
        if has_export_verb and re.search(r"\b(?:pdf|пдф)\b|\bв\s+pdf\b", lowered):
            export_kind = "pdf"
        elif has_export_verb and re.search(r"\b(?:excel|xlsx|таблиц)\b|\bв\s+excel\b", lowered):
            export_kind = "excel"
    if export_kind not in {"pdf", "excel"}:
        return None
    if not bool(_LIVE_DATA_RE.search(user_message or "")):
        return None

    query = str(user_message or "").strip()
    if not query:
        return None

    if export_kind == "pdf":
        export_tool = "pdf_create"
        file_name = "weather-report.pdf"
    else:
        export_tool = "excel_create"
        file_name = "weather-report.xlsx"

    return RouterOutput(
        decision=RouterDecision.TOOL,
        steps=[
            ToolStep(tool="web_search", arguments={"query": query}),
            ToolStep(
                tool=export_tool,
                arguments={
                    "title": "Актуальные данные",
                    "filename": file_name,
                    "content": "$prev.body",
                },
            ),
        ],
        response_hint="Сначала получи актуальные данные, затем сформируй файл",
        confidence=0.55,
    )
