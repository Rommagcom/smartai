from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.graph.node_helpers import (
    WEB_SEARCH_RE,
    _hard_structured_route,
    deterministic_route,
    extract_router_output_from_exception,
    fallback_explicit_export_route,
    fallback_live_data_export_route,
    feedback_requires_web_search,
    feedback_to_search_query,
    followup_export_route,
    is_live_data_query,
    is_web_search_intent,
    load_user_tool_context,
    strip_web_search_prefix,
)
from app.schemas.graph import RouterDecision, RouterOutput, ToolStep

logger = logging.getLogger(__name__)
_WEB_SEARCH_HINT = "Выполни поиск в интернете"


def _dev_log(event: str, **ctx: Any) -> None:
    if not settings.DEV_VERBOSE_LOGGING:
        return
    logger.info(
        f"graph node: {event}",
        extra={"context": {"component": "langgraph", "event": event, **ctx}},
    )


def _build_retrieved_tools_block(retrieved_tools: list[dict]) -> str:
    if not retrieved_tools:
        return ""

    def _format_hit(hit: dict) -> str | None:
        from app.schemas.tool_registry import RetrievedTool

        try:
            tool = RetrievedTool(**hit)
        except Exception:
            return None
        return f"  - {tool.to_planner_signature()} [score={tool.score:.2f}]"

    lines: list[str] = []
    for hit in retrieved_tools:
        line = _format_hit(hit)
        if line:
            lines.append(line)

    if not lines:
        return ""

    return (
        "\nСемантически найденные инструменты (наиболее релевантны запросу):\n"
        + "\n".join(lines)
        + "\n"
    )


def _build_router_prompt(
    *,
    user_message: str,
    feedback_plan: str,
    tool_catalog_signatures: str,
    dynamic_tools_block: str,
    integrations_block: str,
    retrieved_block: str,
) -> str:
    return (
        "Ты — маршрутизатор задач AI-агента.\n"
        "Твоя цель — выбрать правильный инструмент для выполнения запроса.\n\n"
        f"Вопрос пользователя: \"{user_message}\"\n\n"
        "Доступные инструменты:\n"
        f"{tool_catalog_signatures}\n"
        f"{dynamic_tools_block}"
        f"{integrations_block}"
        f"{retrieved_block}"
        "(Также всегда доступен инструмент 'web_search' для поиска в интернете)\n\n"
        "ОБРАТИ ВНИМАНИЕ НА ЗАМЕЧАНИЯ ПРЕДЫДУЩЕГО ШАГА:\n"
        f"{feedback_plan if feedback_plan else 'Это первый проход, замечаний нет.'}\n\n"
        "ИНСТРУКЦИЯ:\n"
        "Если в замечаниях сказано искать в интернете — выбирай decision='web_search' "
        "и формируй оптимальный query в steps.\n"
        "Если нужно дернуть внутреннее API/инструменты — выбирай decision='tool'.\n"
        "Если нужен обычный ответ без инструментов — decision='chat'.\n\n"
        "Верни JSON с полями: decision, steps, response_hint, confidence.\n"
        "decision: 'tool' — нужен инструмент, 'chat' — обычный разговор, "
        "'memory' — операция с памятью, 'clarify' — нужно уточнение, "
        "'web_search' — поиск информации в интернете.\n"
        "Правила:\n"
        "1) Для напоминаний используй cron_add с schedule_text и task_text. "
        "Если задача требует вызова API/интеграции (курс валют, погода и т.д.) — добавь action_type='chat'. "
        "Если обычное текстовое напоминание — action_type не нужен.\n"
        "2) Для PDF — pdf_create.\n"
        "2a) Для Excel/таблицы — excel_create.\n"
        "3) Если просит подключить API — register_api_tool с user_message (полным сообщением пользователя).\n"
        "4) Для удаления всех напоминаний — cron_delete_all.\n"
        "5) Не выдумывай аргументы.\n"
        "6) steps — максимум 5 шагов.\n"
        "7) Для удаления факта: memory_search → memory_delete.\n"
        "8) Для ВЫЗОВА подключённой интеграции используй integration_call "
        "с service_name из списка интеграций пользователя. "
        "Если пользователь пишет 'вызови интеграцию X', 'данные из X', 'курс валют из nationalbank' — "
        "это integration_call с service_name=X.\n"
        "9) Для пользовательских динамических API используй dyn:<имя> с нужными аргументами.\n"
        "10) Если в 'семантически найденных инструментах' есть подходящий — предпочитай его.\n"
        "11) Для списка загруженных документов — doc_list.\n"
        "12) Для удаления одного документа — doc_delete с source_doc (имя файла).\n"
        "13) Для удаления всех документов — doc_delete_all.\n"
        "14) Для поиска информации в интернете используй decision='web_search' с query в steps. "
        "Если пользователь просит 'найди в интернете', 'загугли', 'поищи в сети' — это web_search. "
        "Для регулярного получения данных из интернета — cron_add с action_type='chat' и task_text='найди в интернете ...'. \n"
        "15) Если шаг зависит от результата предыдущего, используй плейсхолдеры: "
        "$prev.body — тело ответа предыдущего шага, $prev.items, $prev.content и т.д. "
        "Пример: [{\"tool\": \"integration_call\", \"arguments\": {\"service_name\": \"X\"}}, "
        "{\"tool\": \"pdf_create\", \"arguments\": {\"title\": \"Отчёт\", \"content\": \"$prev.body\"}}].\n"
        "15a) Для одиночного шага pdf_create/excel_create НЕ пиши полный документ в arguments.content. "
        "Передавай только короткий источник (до 300 символов, без markdown-блоков и длинных переносов). "
        "Если нужен полный документ, сначала получи/сформируй данные отдельным шагом, затем используй $prev.body.\n"
        "16) Если пользователь просит актуальные данные (погода, курс валют, новости и т.п.) И одновременно экспорт в PDF/Excel, "
        "сначала получи данные (decision='web_search' или integration_call), затем сформируй файл по результатам. "
        "Нельзя сразу делать pdf_create/excel_create только из исходного текста запроса.\n"
    )


def _try_feedback_web_search_route(user_message: str, feedback_plan: str) -> dict | None:
    if not (feedback_plan and feedback_requires_web_search(feedback_plan)):
        return None
    query = feedback_to_search_query(feedback_plan, user_message)
    reflexion_route = RouterOutput(
        decision=RouterDecision.WEB_SEARCH,
        steps=[ToolStep(tool="web_search", arguments={"query": query})],
        response_hint="Следующий цикл: web_search по плану доработки",
        confidence=0.95,
    )
    return {"router_output": reflexion_route, "next_step": "web_search"}


def _try_hard_route(user_message: str) -> dict | None:
    hard_route = _hard_structured_route(user_message)
    if hard_route is None:
        return None
    _dev_log("router_hard_structured", steps_count=len(hard_route.steps))
    return {
        "router_output": hard_route,
        "next_step": hard_route.decision.value,
    }


def _try_export_followup_route(user_message: str, history: list[dict]) -> dict | None:
    if not settings.ROUTER_ENABLE_EXPORT_FOLLOWUP_SHORTCUT:
        return None
    export_followup = followup_export_route(user_message, history)
    if export_followup is None:
        return None
    _dev_log("router_deterministic_export_followup", decision=export_followup.decision.value)
    return {
        "router_output": export_followup,
        "next_step": export_followup.decision.value,
    }


def _try_deterministic_route(user_message: str) -> dict | None:
    if not settings.ROUTER_ENABLE_DETERMINISTIC_SHORTCUTS:
        return None
    deterministic = deterministic_route(user_message)
    if deterministic is None:
        return None
    _dev_log("router_deterministic", decision=deterministic.decision.value)
    return {
        "router_output": deterministic,
        "next_step": deterministic.decision.value,
    }


async def _load_router_context(user_id: Any) -> tuple[str, str]:
    if not user_id:
        return "", ""
    return await load_user_tool_context(user_id)


def _try_live_data_route(
    *,
    user_id: Any,
    user_message: str,
    integrations_block: str,
    dynamic_tools_block: str,
) -> dict | None:
    if not (
        user_id
        and not integrations_block.strip()
        and not dynamic_tools_block.strip()
        and is_live_data_query(user_message)
    ):
        return None
    query = user_message.strip()
    live_data_route = RouterOutput(
        decision=RouterDecision.WEB_SEARCH,
        steps=[ToolStep(tool="web_search", arguments={"query": query})],
        response_hint="Быстрый путь live-data: web_search без planner LLM",
        confidence=0.9,
    )
    _dev_log("router_fast_live_data", query=query[:120])
    return {"router_output": live_data_route, "next_step": "web_search"}


def _build_router_messages(*, planner_prompt: str, history: list[dict], user_message: str) -> list[dict[str, str]]:
    router_messages: list[dict[str, str]] = [{"role": "system", "content": planner_prompt}]
    recent = history[-settings.CONTEXT_ALWAYS_KEEP_LAST_MESSAGES:]
    if recent:
        router_messages.extend(recent)
    router_messages.append({"role": "user", "content": user_message})
    return router_messages


def _try_live_export_override(user_message: str, router_output: RouterOutput) -> dict | None:
    if not (
        settings.ROUTER_OVERRIDE_CLARIFY_LIVE_EXPORT
        and settings.ROUTER_ENABLE_LIVE_EXPORT_FALLBACK
        and router_output.decision in {RouterDecision.CLARIFY, RouterDecision.CHAT}
    ):
        return None
    live_export_override = fallback_live_data_export_route(user_message)
    if live_export_override is None:
        return None
    _dev_log(
        "router_override_live_export",
        original_decision=router_output.decision.value,
        steps_count=len(live_export_override.steps),
    )
    return {
        "router_output": live_export_override,
        "next_step": "tool",
    }


async def _run_router_llm(
    *,
    llm_provider: Any,
    router_messages: list[dict[str, str]],
    planner_model: str | None,
) -> RouterOutput:
    return await llm_provider.chat_structured(
        messages=router_messages,
        response_model=RouterOutput,
        model=planner_model,
        temperature=settings.LITELLM_PLANNER_TEMPERATURE,
        max_tokens=settings.OLLAMA_NUM_PREDICT_PLANNER,
    )


def _build_web_search_fallback(user_message: str) -> dict:
    query = strip_web_search_prefix(user_message)
    _dev_log("router_fallback_web_search", query=query[:120])
    fallback = RouterOutput(
        decision=RouterDecision.WEB_SEARCH,
        steps=[ToolStep(tool="web_search", arguments={"query": query})],
        response_hint=_WEB_SEARCH_HINT,
        confidence=0.5,
    )
    return {
        "router_output": fallback,
        "next_step": "web_search",
    }


def _handle_router_fallback(exc: Exception, *, user_message: str) -> dict:
    logger.warning("Router LLM failed: %s, using fallback routing", exc)
    salvaged = extract_router_output_from_exception(
        exc,
        user_message,
        web_search_pattern=WEB_SEARCH_RE,
        web_search_hint=_WEB_SEARCH_HINT,
    )
    if salvaged is not None:
        _dev_log(
            "router_fallback_salvaged",
            decision=salvaged.decision.value,
            steps_count=len(salvaged.steps),
        )
        return {
            "router_output": salvaged,
            "next_step": salvaged.decision.value,
        }

    explicit_export_fallback = fallback_explicit_export_route(user_message)
    if explicit_export_fallback is not None:
        _dev_log(
            "router_fallback_explicit_export",
            decision=explicit_export_fallback.decision.value,
            steps_count=len(explicit_export_fallback.steps),
        )
        return {
            "router_output": explicit_export_fallback,
            "next_step": explicit_export_fallback.decision.value,
        }

    if settings.ROUTER_ENABLE_DETERMINISTIC_FALLBACKS:
        if settings.ROUTER_ENABLE_LIVE_EXPORT_FALLBACK:
            live_export_fallback = fallback_live_data_export_route(user_message)
            if live_export_fallback is not None:
                _dev_log(
                    "router_fallback_live_export",
                    decision=live_export_fallback.decision.value,
                    steps_count=len(live_export_fallback.steps),
                )
                return {
                    "router_output": live_export_fallback,
                    "next_step": "tool",
                }
        if is_web_search_intent(user_message):
            return _build_web_search_fallback(user_message)

    fallback = RouterOutput(decision=RouterDecision.CHAT, confidence=0.3)
    return {
        "router_output": fallback,
        "next_step": "chat",
    }


async def router_node(state: dict) -> dict:
    """Classify user intent and decide the next step."""
    from app.llm import llm_provider
    from app.services.tool_catalog_service import tool_catalog_service

    user_message = state["user_message"]
    user_id = state.get("user_id")
    feedback_plan = str(state.get("feedback_plan") or "").strip()
    retrieved_tools: list[dict] = state.get("retrieved_tools") or []
    history: list[dict] = state.get("history_messages") or []
    _dev_log("router_start", message_preview=user_message[:120])

    for fast_path in (
        _try_feedback_web_search_route(user_message, feedback_plan),
        _try_hard_route(user_message),
        _try_export_followup_route(user_message, history),
        _try_deterministic_route(user_message),
    ):
        if fast_path is not None:
            return fast_path

    integrations_block, dynamic_tools_block = await _load_router_context(user_id)
    live_data_route = _try_live_data_route(
        user_id=user_id,
        user_message=user_message,
        integrations_block=integrations_block,
        dynamic_tools_block=dynamic_tools_block,
    )
    if live_data_route is not None:
        return live_data_route

    retrieved_block = _build_retrieved_tools_block(retrieved_tools)
    planner_model = settings.LITELLM_PLANNER_MODEL or None
    planner_prompt = _build_router_prompt(
        user_message=user_message,
        feedback_plan=feedback_plan,
        tool_catalog_signatures=tool_catalog_service.planner_signatures(),
        dynamic_tools_block=dynamic_tools_block,
        integrations_block=integrations_block,
        retrieved_block=retrieved_block,
    )

    router_messages = _build_router_messages(
        planner_prompt=planner_prompt,
        history=history,
        user_message=user_message,
    )

    try:
        router_output = await _run_router_llm(
            llm_provider=llm_provider,
            router_messages=router_messages,
            planner_model=planner_model,
        )
        _dev_log(
            "router_llm",
            decision=router_output.decision.value,
            steps_count=len(router_output.steps),
            confidence=router_output.confidence,
        )
        live_export_override = _try_live_export_override(user_message, router_output)
        if live_export_override is not None:
            return live_export_override
        return {
            "router_output": router_output,
            "next_step": router_output.decision.value,
        }
    except Exception as exc:
        return _handle_router_fallback(exc, user_message=user_message)