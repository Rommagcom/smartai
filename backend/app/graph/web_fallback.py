from __future__ import annotations

from typing import Any, Callable

from app.graph.tool_result_formatter import build_raw_tool_summary
from app.schemas.graph import ToolResult


def web_result_field(item: Any, key: str) -> str:
    """Safe field extraction from web result entries (dict/object/string)."""
    if isinstance(item, dict):
        return str(item.get(key) or "").strip()
    value = getattr(item, key, "")
    if value:
        return str(value).strip()
    if key == "title" and item is not None:
        return str(item).strip()
    return ""


def build_raw_web_summary(
    web_fetch_content: str,
    web_search_results: list[dict],
    tool_results: list[ToolResult],
    sanitize_answer: Callable[[str], str],
) -> str:
    """Fallback answer when compose JSON mode fails but web/search context exists."""
    if web_fetch_content and web_fetch_content.strip():
        return sanitize_answer(web_fetch_content.strip()[:2500])
    if web_search_results:
        lines = ["Найдено в интернете:"]
        for result in web_search_results[:5]:
            title = web_result_field(result, "title") or "Без названия"
            snippet = web_result_field(result, "snippet")
            url = web_result_field(result, "url")
            lines.append(f"- {title}: {snippet} ({url})")
        return "\n".join(lines)
    return build_raw_tool_summary(tool_results)
