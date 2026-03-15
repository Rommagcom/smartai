from __future__ import annotations

import json

from app.schemas.graph import ToolResult


def format_deterministic_tool_answer(tool_results: list[ToolResult]) -> str | None:
    """Format known tool results without LLM."""
    # Data-fetching tools should go through compose so LLM can summarize payloads.
    data_tools = {"integration_call", "dynamic_tool_call"}
    tools_in_chain = {tr.tool for tr in tool_results if tr.success}
    if tools_in_chain & data_tools:
        return None

    for tr in tool_results:
        if not tr.success or not tr.result:
            continue
        if tr.tool == "pdf_create":
            status = str(tr.result.get("status") or "").strip().lower()
            message = str(tr.result.get("message") or "").strip()
            if status in {"queued", "deduplicated"}:
                return message or "Задача поставлена в очередь."
            fname = tr.result.get("file_name") or "document.pdf"
            size = tr.result.get("size_bytes") or 0
            size_kb = f" ({size / 1024:.1f} KB)" if size else ""
            return f"Документ {fname} в процессе создания{size_kb}."
        if tr.tool == "excel_create":
            status = str(tr.result.get("status") or "").strip().lower()
            message = str(tr.result.get("message") or "").strip()
            if status in {"queued", "deduplicated"}:
                return message or "Задача поставлена в очередь."
            fname = tr.result.get("file_name") or "document.xlsx"
            size = tr.result.get("size_bytes") or 0
            size_kb = f" ({size / 1024:.1f} KB)" if size else ""
            return f"Документ {fname} в процессе создания{size_kb}."
        if tr.tool == "cron_add":
            payload = tr.result.get("payload", {})
            if isinstance(payload, dict):
                task = payload.get("message", "")
                cron_expr = tr.result.get("cron_expression", "")
                action = tr.result.get("action_type", "send_message")
                if task:
                    suffix = f" ({cron_expr})" if cron_expr else ""
                    if action == "chat":
                        return f"Задача запланирована: {task}{suffix}\nПо расписанию я выполню запрос и пришлю результат."
                    return f"Напоминание создано: {task}{suffix}"
        if tr.tool == "cron_list":
            items = tr.result.get("items", [])
            if isinstance(items, list):
                if not items:
                    return "У вас пока нет активных напоминаний."
                lines = ["Ваши напоминания:"]
                for item in items[:20]:
                    if isinstance(item, dict):
                        name = item.get("name", "")
                        cron = item.get("cron_expression", "")
                        lines.append(f"- {name} ({cron})")
                return "\n".join(lines)
        if tr.tool == "cron_delete_all":
            return "Все напоминания удалены."
        if tr.tool == "memory_delete_all":
            return "Память очищена."
        if tr.tool == "doc_list":
            items = tr.result.get("items", [])
            if isinstance(items, list):
                if not items:
                    return "У вас нет загруженных документов."
                lines = ["Ваши документы:"]
                for item in items[:20]:
                    if isinstance(item, dict):
                        name = item.get("source_doc") or item.get("name") or "?"
                        chunks = item.get("chunk_count", "")
                        suffix = f" ({chunks} частей)" if chunks else ""
                        lines.append(f"- {name}{suffix}")
                return "\n".join(lines)
        if tr.tool == "doc_delete":
            source = tr.result.get("source_doc", "")
            deleted_chunks = tr.result.get("deleted_chunks", 0)
            if not tr.result.get("deleted"):
                return f"Документ {source} не найден." if source else "Документ не найден."
            return f"Документ {source} удалён ({deleted_chunks} частей)."
        if tr.tool == "doc_delete_all":
            deleted = tr.result.get("deleted_count", 0)
            if deleted <= 0:
                return "У вас не было загруженных документов."
            return f"Все документы удалены ({deleted} частей)."
        if tr.tool == "dynamic_tool_register":
            msg = tr.result.get("message", "")
            if msg:
                return msg
        if tr.tool == "register_api_tool":
            msg = tr.result.get("message", "")
            if msg:
                return msg
            status = tr.result.get("status", "")
            tool_info = tr.result.get("tool", {})
            name = tool_info.get("name", "unknown") if isinstance(tool_info, dict) else "unknown"
            return f"Инструмент {name} {'обновлён' if status == 'updated' else 'зарегистрирован'}."
        if tr.tool == "dynamic_tool_list":
            items = tr.result.get("items", [])
            if isinstance(items, list):
                if not items:
                    return "У вас пока нет зарегистрированных пользовательских API."
                lines = ["Ваши пользовательские API-инструменты:"]
                for item in items[:20]:
                    if isinstance(item, dict):
                        name = item.get("name", "")
                        desc = item.get("description", "")
                        endpoint = item.get("endpoint", "")
                        lines.append(f"- **{name}**: {desc} ({endpoint})")
                return "\n".join(lines)
        if tr.tool == "dynamic_tool_delete":
            tool_name = str(tr.result.get("tool_name") or "").strip()
            if tool_name:
                return f"Пользовательский API-инструмент '{tool_name}' удалён."
            return "Пользовательский API-инструмент удалён."
        if tr.tool == "dynamic_tool_delete_all":
            count = tr.result.get("deleted_count", 0)
            return f"Все пользовательские API-инструменты удалены ({count})."
        if tr.tool == "dynamic_tool_call" or str(tr.tool).startswith("dyn:") or str(tr.tool).startswith("dyn_"):
            # Let compose_node handle rich formatting via LLM
            pass
        if tr.tool == "memory_list":
            items = tr.result.get("items", [])
            if isinstance(items, list):
                if not items:
                    return "Память пуста."
                lines = ["Ваша память:"]
                for item in items[:20]:
                    if isinstance(item, dict):
                        content = item.get("content", "")
                        lines.append(f"- {content}")
                return "\n".join(lines)
    return None


def build_raw_tool_summary(tool_results: list[ToolResult]) -> str:
    """Last-resort summary from raw tool output."""
    parts: list[str] = []
    for tr in tool_results:
        if not tr.success or not tr.result:
            continue

        if tr.tool == "integration_call":
            status_code = int(tr.result.get("status_code") or 0)
            body = str(tr.result.get("body") or "").strip()
            if body:
                parts.append(
                    f"Интеграция вернула данные (HTTP {status_code}), "
                    "но авто-форматирование ответа не удалось. "
                    "Попробуйте уточнить запрос (например: 'покажи только USD')."
                )
                continue

        msg = tr.result.get("message", "")
        if msg:
            parts.append(str(msg)[:8000])
        elif tr.result:
            dump_data = tr.result
            if "body" in tr.result and "headers" in tr.result:
                dump_data = {k: v for k, v in tr.result.items() if k != "headers"}
            parts.append(json.dumps(dump_data, ensure_ascii=False, default=str)[:8000])

    if not parts:
        return "Не удалось получить данные. Повторите запрос позже."
    return "Результат:\n\n" + "\n\n".join(parts)
