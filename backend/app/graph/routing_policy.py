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
