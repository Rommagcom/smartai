"""LangGraph agent nodes — each node is a pure function operating on AgentState.

Node architecture:
  ┌─────────┐
  │guardrail│ → input safety check
  ├─────────┤
  │ memory  │ → gather all memory layers
  ├─────────┤
    │ intent  │ → micro-router: small_talk | needs_tools
    ├─────────┤
  │retriever│ → Milvus semantic tool search
  ├─────────┤
  │ router  │ → decide: tool | chat | memory | clarify | web_search
  ├─────────┤
  │tool_exec│ → execute tool chain
  ├─────────┤
  │web_srch │ → DuckDuckGo search (graph node)
  ├─────────┤
  │web_fetch│ → trafilatura page extraction
  ├─────────┤
  │  chat   │ → generate final answer via LLM
  ├─────────┤
  │ compose │ → compose answer from tool/web results
  ├─────────┤
  │ output  │ → output guardrail + STM append
  └─────────┘

Each node takes AgentState dict, returns partial state updates.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.config import settings
from app.graph.node_helpers import (
    WEB_SEARCH_RE,
    _build_enriched_system_prompt,
    _hard_structured_route,
    _looks_like_small_talk,
    _sanitize_llm_answer,
    apply_direct_route_fallback,
    apply_inline_cron_bridge,
    apply_inline_integration_bridge,
    apply_output_guardrail,
    build_raw_tool_summary,
    build_raw_web_summary,
    deterministic_route,
    enqueue_export_if_needed,
    extract_artifacts,
    extract_facts_to_ltm,
    extract_non_json_answer_from_exception,
    extract_router_output_from_exception,
    fallback_live_data_export_route,
    feedback_requires_web_search,
    feedback_to_search_query,
    followup_export_route,
    format_deterministic_tool_answer,
    has_successful_export_call,
    is_web_search_intent,
    load_user_tool_context,
    requested_export_kind,
    sanitize_false_attachment_claims,
    should_reenqueue_export,
    strip_web_search_prefix,
    web_result_field,
)
from app.schemas.graph import (
    AgentState,
    GuardrailResult,
    GuardrailVerdict,
    IntegrationCallArgs,
    IntegrationInfo,
    RouterDecision,
    RouterOutput,
    ToolResult,
    ToolStep,
)

logger = logging.getLogger(__name__)
_WEB_SEARCH_HINT = "Выполни поиск в интернете"


class IntentClassifierOutput(BaseModel):
    intent: Literal["small_talk", "needs_tools"] = Field(
        description="Lightweight intent class: small_talk or needs_tools"
    )


class ReflexionComposeOutput(BaseModel):
    is_complete: bool
    answer: str = ""
    feedback_plan: str = ""


def _dev_log(event: str, **ctx: Any) -> None:
    if not settings.DEV_VERBOSE_LOGGING:
        return
    logger.info(
        f"graph node: {event}",
        extra={"context": {"component": "langgraph", "event": event, **ctx}},
    )


# ======================================================================
# Node: Input Guardrail
# ======================================================================


async def input_guardrail_node(state: dict) -> dict:
    """Check user input for safety issues before processing."""
    if not settings.GUARDRAILS_ENABLED:
        return {"input_guardrail": GuardrailResult(verdict=GuardrailVerdict.PASS)}

    user_message = state.get("user_message", "")

    # Length check
    if len(user_message) > settings.GUARDRAILS_MAX_INPUT_LENGTH:
        return {
            "input_guardrail": GuardrailResult(
                verdict=GuardrailVerdict.BLOCK,
                reason=f"Message exceeds max length ({settings.GUARDRAILS_MAX_INPUT_LENGTH} chars)",
            ),
            "final_answer": "Сообщение слишком длинное. Пожалуйста, сократите запрос.",
            "next_step": "end",
        }

    # Prompt injection detection
    if settings.GUARDRAILS_BLOCK_PROMPT_INJECTION:
        from app.guardrails import prompt_shield
        result = prompt_shield.check_input(user_message)
        if result.verdict == GuardrailVerdict.BLOCK:
            return {
                "input_guardrail": result,
                "final_answer": "Запрос отклонён системой безопасности.",
                "next_step": "end",
            }
        return {"input_guardrail": result}

    return {"input_guardrail": GuardrailResult(verdict=GuardrailVerdict.PASS)}


# ======================================================================
# Node: Memory Gathering
# ======================================================================


async def memory_node(state: dict) -> dict:
    """Gather context from all memory layers."""
    from app.memory import memory_manager
    from app.db.session import AsyncSessionLocal

    user_id = state["user_id"]
    session_id = state["session_id"]
    user_message = state["user_message"]

    _dev_log("memory_gather_start", user_id=str(user_id))

    async with AsyncSessionLocal() as db:
        context = await memory_manager.gather_context(
            db=db,
            user_id=user_id,
            session_id=session_id,
            user_message=user_message,
        )

        # Entity extraction (semantic memory)
        entities = await memory_manager.extract_entities(user_message)
        if entities:
            await memory_manager.store_entities(db, user_id, entities)
            await db.commit()

    _dev_log(
        "memory_gather_done",
        history_count=len(context["history_messages"]),
        stm_count=len(context["stm_context"]),
        ltm_count=len(context["ltm_context"]),
        rag_count=len(context["rag_context"]),
        entities_count=len(entities),
    )

    return {
        "history_messages": context["history_messages"],
        "stm_context": context["stm_context"],
        "ltm_context": context["ltm_context"],
        "rag_context": context["rag_context"],
        "history_summary": context["history_summary"],
        "extracted_entities": entities,
    }


# ======================================================================
# Node: Intent Classifier (micro-router)
# ======================================================================


async def intent_classifier_node(state: dict) -> dict:
    """Classify if request can skip heavy retrieval/tool routing."""
    from app.llm import llm_provider

    messages: list[str] = state.get("messages") or []
    query = (messages[-1] if messages else state.get("user_message", "")).strip()
    if not query:
        return {"intent": "small_talk", "next_step": "chat"}

    # Deterministic guard for ultra-cheap path.
    if _looks_like_small_talk(query):
        return {"intent": "small_talk", "next_step": "chat"}

    prompt = (
        "Твоя задача — классифицировать намерение пользователя.\n\n"
        f"Входящее сообщение: \"{query}\"\n\n"
        "ПРАВИЛА КЛАССИФИКАЦИИ:\n"
        "1. \"small_talk\": простое приветствие, прощание, благодарность, "
        "короткая светская беседа или запрос, который НЕ требует поиска свежих фактов, "
        "баз данных или вычислений.\n"
        "2. \"needs_tools\": запрос, требующий поиска информации (интернет), "
        "корпоративных данных, использования API, аналитики или точных фактов.\n\n"
        "Ответь СТРОГО валидным JSON формата:\n"
        '{"intent": "small_talk" | "needs_tools"}'
    )

    try:
        out = await llm_provider.chat_structured(
            messages=[{"role": "system", "content": prompt}],
            response_model=IntentClassifierOutput,
            model=settings.LITELLM_PLANNER_MODEL or None,
            temperature=0.0,
            max_tokens=120,
        )
        intent = out.intent
    except Exception as exc:
        logger.warning("Intent classifier failed: %s", exc)
        intent = "small_talk" if _looks_like_small_talk(query) else "needs_tools"

    return {
        "intent": intent,
        "next_step": "chat" if intent == "small_talk" else "retriever",
    }


# ======================================================================
# Node: Tool Retriever (Milvus semantic search)
# ======================================================================


async def tool_retriever_node(state: dict) -> dict:
    """Search Milvus for relevant tools based on the user query.

    This node runs BEFORE the router so the planner LLM only sees
    tools that are semantically relevant to the current request,
    enabling the system to scale to thousands of tools.
    """
    from app.services.vector_tool_registry import vector_tool_registry

    user_message = state["user_message"]
    user_id = state.get("user_id")

    if not user_id:
        return {"retrieved_tools": []}

    _dev_log("retriever_start", user_id=str(user_id))

    try:
        hits = await vector_tool_registry.get_relevant_tools(
            user_query=user_message,
            user_id=user_id,
            top_k=settings.TOOL_RETRIEVER_TOP_K,
        )
        _dev_log("retriever_done", hits_count=len(hits))
        return {"retrieved_tools": hits}
    except Exception as exc:
        logger.warning("Tool retriever failed: %s", exc)
        return {"retrieved_tools": []}


# ======================================================================
# Node: Router / Planner (LLM-based intent classification)
# ======================================================================


async def router_node(state: dict) -> dict:
    """Classify user intent and decide the next step.

    Uses LiteLLM with structured output to guarantee valid RouterOutput.
    Falls back to deterministic pattern matching if LLM fails.

    The planner prompt includes:
    - Static built-in tools from skills_registry
    - User integrations (from DB)
    - User dynamic tools (from DB)
    - **Semantically retrieved tools** (from Milvus via retriever node)
    """
    from app.llm import llm_provider
    from app.services.skills_registry_service import skills_registry_service

    user_message = state["user_message"]
    user_id = state.get("user_id")
    feedback_plan = str(state.get("feedback_plan") or "").strip()
    retrieved_tools: list[dict] = state.get("retrieved_tools") or []
    history: list[dict] = state.get("history_messages") or []
    _dev_log("router_start", message_preview=user_message[:120])

    # Reflexion fast-path: follow explicit previous-cycle instruction.
    if feedback_plan and feedback_requires_web_search(feedback_plan):
        query = feedback_to_search_query(feedback_plan, user_message)
        reflexion_route = RouterOutput(
            decision=RouterDecision.WEB_SEARCH,
            steps=[ToolStep(tool="web_search", arguments={"query": query})],
            response_hint="Следующий цикл: web_search по плану доработки",
            confidence=0.95,
        )
        return {
            "router_output": reflexion_route,
            "next_step": "web_search",
        }

    # 0. Hard structured command fast-path (graph-only command contract).
    # This path is intentionally independent from generic deterministic shortcuts.
    hard_route = _hard_structured_route(user_message)
    if hard_route is not None:
        _dev_log("router_hard_structured", steps_count=len(hard_route.steps))
        return {
            "router_output": hard_route,
            "next_step": hard_route.decision.value,
        }

    # 1. History-aware export follow-up should always work, even when generic
    # deterministic shortcuts are disabled by config.
    if settings.ROUTER_ENABLE_EXPORT_FOLLOWUP_SHORTCUT:
        export_followup = followup_export_route(user_message, history)
        if export_followup is not None:
            _dev_log("router_deterministic_export_followup", decision=export_followup.decision.value)
            return {
                "router_output": export_followup,
                "next_step": export_followup.decision.value,
            }

    # 2. Try deterministic shortcuts first (fast path, no LLM call)
    if settings.ROUTER_ENABLE_DETERMINISTIC_SHORTCUTS:

        deterministic = deterministic_route(user_message)
        if deterministic is not None:
            _dev_log("router_deterministic", decision=deterministic.decision.value)
            return {
                "router_output": deterministic,
                "next_step": deterministic.decision.value,
            }

    # 3. Load user integrations & dynamic tools for context
    integrations_block = ""
    dynamic_tools_block = ""
    if user_id:
        integrations_block, dynamic_tools_block = await load_user_tool_context(user_id)

    # 4. Build retrieved-tools block from Milvus results
    retrieved_block = ""
    if retrieved_tools:
        from app.schemas.tool_registry import RetrievedTool
        lines = []
        for hit in retrieved_tools:
            try:
                rt = RetrievedTool(**hit)
                lines.append(f"  - {rt.to_planner_signature()} [score={rt.score:.2f}]")
            except Exception:
                continue
        if lines:
            retrieved_block = (
                "\nСемантически найденные инструменты (наиболее релевантны запросу):\n"
                + "\n".join(lines) + "\n"
            )

    # 5. LLM-based routing via structured output
    planner_model = settings.LITELLM_PLANNER_MODEL or None
    planner_prompt = (
        "Ты — маршрутизатор задач AI-агента.\n"
        "Твоя цель — выбрать правильный инструмент для выполнения запроса.\n\n"
        f"Вопрос пользователя: \"{user_message}\"\n\n"
        "Доступные инструменты:\n"
        f"{skills_registry_service.planner_signatures()}\n"
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
        "Для регулярного получения данных из интернета — cron_add с action_type='chat' и task_text='найди в интернете ...'. "
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

    # Build messages with recent history for context continuity
    router_messages: list[dict[str, str]] = [{"role": "system", "content": planner_prompt}]
    # Include last few messages so the router understands references like
    # "сделай то же самое", "а теперь в PDF", etc.
    recent = history[-settings.CONTEXT_ALWAYS_KEEP_LAST_MESSAGES:]
    if recent:
        router_messages.extend(recent)
    router_messages.append({"role": "user", "content": user_message})

    try:
        router_output = await llm_provider.chat_structured(
            messages=router_messages,
            response_model=RouterOutput,
            model=planner_model,
            temperature=settings.LITELLM_PLANNER_TEMPERATURE,
            max_tokens=settings.OLLAMA_NUM_PREDICT_PLANNER,
        )
        _dev_log(
            "router_llm",
            decision=router_output.decision.value,
            steps_count=len(router_output.steps),
            confidence=router_output.confidence,
        )
        if (
            settings.ROUTER_OVERRIDE_CLARIFY_LIVE_EXPORT
            and settings.ROUTER_ENABLE_LIVE_EXPORT_FALLBACK
            and router_output.decision in {RouterDecision.CLARIFY, RouterDecision.CHAT}
        ):
            live_export_override = fallback_live_data_export_route(user_message)
            if live_export_override is not None:
                _dev_log(
                    "router_override_live_export",
                    original_decision=router_output.decision.value,
                    steps_count=len(live_export_override.steps),
                )
                return {
                    "router_output": live_export_override,
                    "next_step": "tool",
                }
        return {
            "router_output": router_output,
            "next_step": router_output.decision.value,
        }
    except Exception as exc:
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
            # If the message clearly asks for web search, don't lose the intent
            if is_web_search_intent(user_message):
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
        fallback = RouterOutput(decision=RouterDecision.CHAT, confidence=0.3)
        return {
            "router_output": fallback,
            "next_step": "chat",
        }


# ======================================================================
# Node: Tool Execution
# ======================================================================


async def tool_execution_node(state: dict) -> dict:
    """Execute the planned tool chain from the router output."""
    from app.services.tool_orchestrator_service import tool_orchestrator_service
    from app.db.session import AsyncSessionLocal
    from app.models.user import User
    from sqlalchemy import select

    router_output: RouterOutput | None = state.get("router_output")
    if not router_output or not router_output.steps:
        return {"tool_results": [], "next_step": "chat"}

    user_id = state["user_id"]
    _dev_log(
        "tool_exec_start",
        user_id=str(user_id),
        steps=[s.tool for s in router_output.steps],
    )

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if not user:
                return {
                    "tool_results": [],
                    "error": "User not found",
                    "next_step": "chat",
                }

            steps_dicts = [
                {"tool": step.tool, "arguments": step.arguments}
                for step in router_output.steps
            ]

            history_messages = state.get("history_messages") or []
            fallback_export_content = ""
            for item in reversed(history_messages):
                if not isinstance(item, dict):
                    continue
                if str(item.get("role") or "").lower() != "assistant":
                    continue
                content = str(item.get("content") or "").strip()
                if content:
                    fallback_export_content = content
                    break
            if not fallback_export_content:
                fallback_export_content = str(state.get("user_message") or "").strip()

            raw_results = await tool_orchestrator_service.execute_tool_chain(
                db=db,
                user=user,
                steps=steps_dicts,
                max_steps=settings.LANGGRAPH_MAX_ITERATIONS,
                initial_context={"_fallback_export_content": fallback_export_content},
            )
            await db.commit()
    except Exception as exc:
        logger.warning("tool_execution_node failed: %s", exc)
        return {
            "tool_results": [
                ToolResult(
                    tool="system_error",
                    arguments={},
                    success=False,
                    error="Tool execution unavailable",
                )
            ],
            "error": str(exc),
            "next_step": "compose",
        }

    tool_results = [
        ToolResult(
            tool=r.get("tool", ""),
            arguments=r.get("arguments", {}),
            success=bool(r.get("success")),
            result=r.get("result") if isinstance(r.get("result"), dict) else None,
            error=r.get("error"),
        )
        for r in raw_results
    ]

    # Extract artifacts (e.g. PDF base64)
    artifacts = extract_artifacts(raw_results)

    _dev_log(
        "tool_exec_done",
        success_count=sum(1 for t in tool_results if t.success),
        total_count=len(tool_results),
    )

    return {
        "tool_results": tool_results,
        "artifacts": artifacts,
        "tool_calls_log": raw_results,
        "next_step": "compose",
    }


# ======================================================================
# Node: Chat (Direct LLM Response)
# ======================================================================


async def chat_node(state: dict) -> dict:
    """Generate a direct conversational response (no tools)."""
    from app.llm import llm_provider

    user_message = state["user_message"]
    system_prompt = state.get("system_prompt", "")
    history = state.get("history_messages", [])
    stm = state.get("stm_context", [])
    ltm = state.get("ltm_context", [])
    rag = state.get("rag_context", [])
    summary = state.get("history_summary")

    # Build context-enriched system prompt
    enriched_system = _build_enriched_system_prompt(
        system_prompt=system_prompt,
        stm=stm,
        ltm=ltm,
        rag=rag,
        summary=summary,
    )

    messages: list[dict[str, str]] = [{"role": "system", "content": enriched_system}]
    messages.extend(history[-settings.CONTEXT_ALWAYS_KEEP_LAST_MESSAGES:])
    messages.append({"role": "user", "content": user_message})

    _dev_log("chat_start", messages_count=len(messages))

    try:
        answer = await llm_provider.chat(
            messages,
            temperature=settings.LITELLM_TEMPERATURE,
            max_tokens=settings.OLLAMA_NUM_PREDICT,
        )
        # Sanitize
        answer = _sanitize_llm_answer(answer)
    except Exception as exc:
        logger.warning("Chat LLM failed: %s", exc)
        answer = (
            "Сервис генерации ответа сейчас временно недоступен. "
            "Повторите запрос через 10–30 секунд."
        )

    _dev_log("chat_done", answer_length=len(answer))
    return {"final_answer": answer, "next_step": "output", "is_complete": True}


# ======================================================================
# Node: Web Search
# ======================================================================


async def web_search_node(state: dict) -> dict:
    """Run a DuckDuckGo web search based on router-planned query."""
    from app.services.web_search_service import web_search_service

    router_output: RouterOutput | None = state.get("router_output")
    # Extract query from router steps or fall back to user message
    query = ""
    if router_output and router_output.steps:
        for step in router_output.steps:
            args = step.arguments or {}
            query = str(args.get("query") or "").strip()
            if query:
                break

    if not query:
        query = state.get("user_message", "")

    _dev_log("web_search_start", query=query[:120])

    result = await web_search_service.search(query, max_results=5)
    results = result.get("results") or []
    _dev_log("web_search_done", results_count=len(results))

    return {
        "web_search_results": results,
        "next_step": "web_fetch",
    }


# ======================================================================
# Node: Web Fetch (trafilatura content extraction)
# ======================================================================

_MAX_FETCH_PAGES = 3
_FETCH_TIMEOUT = 8
_FETCH_CONCURRENCY = 3


async def web_fetch_node(state: dict) -> dict:
    """Fetch top URLs from web_search_results and extract clean text."""
    import httpx
    import trafilatura

    results: list[dict] = state.get("web_search_results") or []
    if not results:
        _dev_log("web_fetch_skip", reason="no search results")
        return {"web_fetch_content": "", "next_step": "compose"}

    urls = [r["url"] for r in results[:_MAX_FETCH_PAGES] if r.get("url")]
    _dev_log("web_fetch_start", urls=urls)

    semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def _fetch_one(url: str, client: httpx.AsyncClient) -> str | None:
        async with semaphore:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                text = await asyncio.to_thread(
                    trafilatura.extract,
                    resp.text,
                    include_links=True,
                    include_tables=True,
                    output_format="txt",
                )
                if text and text.strip():
                    # Keep snippet reasonable — up to ~3000 chars per page
                    snippet = text[:3000]
                    return f"### {url}\n{snippet}"
            except Exception:
                logger.debug("web_fetch failed for %s", url, exc_info=True)
        return None

    async with httpx.AsyncClient(
        timeout=_FETCH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; SmartAiBot/1.0)"},
    ) as client:
        fetched_parts = await asyncio.gather(
            *[_fetch_one(url, client) for url in urls],
            return_exceptions=False,
        )

    parts = [part for part in fetched_parts if part]

    combined = "\n\n".join(parts) if parts else ""
    _dev_log("web_fetch_done", pages_ok=len(parts), total_len=len(combined))

    return {"web_fetch_content": combined, "next_step": "compose"}


# ======================================================================
# Node: Compose (tool results → final answer)
# ======================================================================


async def compose_node(state: dict) -> dict:
    """Reflexion synth node: either finalize answer or return explicit feedback plan."""
    from app.llm import llm_provider

    messages_state: list[str] = state.get("messages") or []
    user_message = (messages_state[-1] if messages_state else state.get("user_message", "")).strip()
    history: list[dict] = state.get("history_messages") or []
    tool_results: list[ToolResult] = state.get("tool_results", [])
    web_fetch_content: str = state.get("web_fetch_content") or ""
    web_search_results: list[dict] = state.get("web_search_results") or []
    existing_answer = str(state.get("final_answer") or "").strip()
    iterations = int((state.get("iterations") or state.get("iteration") or 0) + 1)
    max_iterations = int(state.get("max_iterations") or settings.LANGGRAPH_MAX_ITERATIONS)
    all_failed = all(not tr.success for tr in tool_results) if tool_results else True
    has_integration = any(str(tr.tool or "") == "integration_call" for tr in tool_results)

    # If chat already produced a direct answer and there is no additional context,
    # do not force a reflexion pass.
    if existing_answer and not tool_results and not web_search_results and not web_fetch_content:
        return {
            "final_answer": existing_answer,
            "is_complete": True,
            "feedback_plan": "",
            "iterations": iterations,
            "iteration": iterations,
        }

    doc_ask_result = next(
        (
            tr.result
            for tr in tool_results
            if tr.success and str(tr.tool or "") == "doc_ask" and isinstance(tr.result, dict)
        ),
        None,
    )
    if isinstance(doc_ask_result, dict):
        doc_answer = str(doc_ask_result.get("answer") or "").strip()
        doc_items = doc_ask_result.get("items", [])
        source_lines: list[str] = []
        if isinstance(doc_items, list):
            for idx, item in enumerate(doc_items[:10], start=1):
                if not isinstance(item, dict):
                    continue
                source_doc = str(item.get("source_doc") or "документ").strip()
                chunk = str(item.get("chunk_text") or "").strip()
                if len(chunk) > 1200:
                    chunk = chunk[:1200].rstrip() + "..."
                score = item.get("score")
                score_text = f"{float(score):.3f}" if isinstance(score, (int, float)) else "n/a"
                source_lines.append(f"{idx}) {source_doc} (score={score_text})")
                if chunk:
                    source_lines.append(chunk)
        doc_context = "\n\n".join(source_lines).strip()
        try:
            llm_doc_answer = await llm_provider.chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Ты формируешь финальный ответ пользователю на основе результатов поиска по его документам. "
                            "Учитывай формулировку вопроса пользователя. "
                            "Не выдумывай факты вне предоставленного контекста. "
                            "Пиши подробно и структурировано. "
                            "В конце добавь раздел 'Источники' с кратким перечислением документов, на которые опираешься."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Вопрос пользователя:\n{user_message}\n\n"
                            f"Черновой ответ инструмента doc_ask:\n{doc_answer or '(пусто)'}\n\n"
                            f"Фрагменты источников:\n{doc_context or '(источники не переданы)'}"
                        ),
                    },
                ],
                temperature=0.0,
                max_tokens=max(int(settings.OLLAMA_NUM_PREDICT), int(settings.OLLAMA_NUM_PREDICT_PLANNER)),
            )
            llm_doc_answer = _sanitize_llm_answer(llm_doc_answer)
            if llm_doc_answer:
                return {
                    "final_answer": llm_doc_answer,
                    "is_complete": True,
                    "feedback_plan": "",
                    "iterations": iterations,
                    "iteration": iterations,
                }
        except Exception as exc:
            logger.warning("Doc ask compose via LLM failed: %s", exc)
            if doc_answer:
                return {
                    "final_answer": _sanitize_llm_answer(doc_answer),
                    "is_complete": True,
                    "feedback_plan": "",
                    "iterations": iterations,
                    "iteration": iterations,
                }

    context_chunks: list[str] = list(state.get("context") or [])

    if web_fetch_content:
        context_chunks.append(web_fetch_content[:12000])
    elif web_search_results:
        snippets = "\n".join(
            f"- {web_result_field(r, 'title')}: {web_result_field(r, 'snippet')} ({web_result_field(r, 'url')})"
            for r in web_search_results[:5]
        )
        if snippets:
            context_chunks.append(f"Сниппеты web_search:\n{snippets}")

    if tool_results:
        sanitised_results = []
        _strip_keys = {"headers", "file_base64", "base64"}
        for t in tool_results:
            d = t.model_dump()
            res = d.get("result")
            if isinstance(res, dict):
                d["result"] = {k: v for k, v in res.items() if k not in _strip_keys}
            sanitised_results.append(d)
        context_chunks.append(
            "Результаты инструментов:\n" + json.dumps(sanitised_results, ensure_ascii=False, default=str)[:16000]
        )

    context_text = "\n\n".join([c for c in context_chunks if str(c).strip()])
    integration_summary_prompt = ""
    if has_integration and not all_failed:
        integration_summary_prompt = (
            "\n\nДОПОЛНИТЕЛЬНО ДЛЯ ОТВЕТОВ ИНТЕГРАЦИЙ:\n"
            "Ты получил ответ от внешнего API (интеграции). "
            "Проанализируй тело ответа и сформируй ЧЕЛОВЕКОЧИТАЕМЫЙ ответ. "
            "Если данные в XML/JSON - извлеки ключевые значения и представь "
            "в удобном виде (таблица, список, текст). "
            "НЕ выводи сырой XML/JSON. НЕ обрезай данные - покажи ВСЕ основные записи. "
            "Если пользователь просил конкретные данные - выдели их."
        )

    prompt = (
        "Ты — финальный проверяющий AI-агента. Твоя задача — проанализировать "
        "вопрос пользователя и собранные данные.\n\n"
        f"Вопрос пользователя: \"{user_message}\"\n"
        f"Собранные данные:\n{context_text if context_text else '(данные отсутствуют)'}\n\n"
        f"Текущая итерация поиска: {iterations} из {max_iterations}.\n\n"
        "ИНСТРУКЦИЯ:\n"
        "1. Оцени, достаточно ли собранных данных для точного, полного и правдивого ответа.\n"
        "2. Если данных ДОСТАТОЧНО (или итерация достигла лимита):\n"
        "   - Сформируй итоговый ответ.\n"
        "   - Установи is_complete: true.\n"
        "   - feedback_plan оставь пустым.\n"
        "3. Если данных НЕДОСТАТОЧНО:\n"
        "   - Не пиши финальный ответ пользователю.\n"
        "   - Напиши четкую инструкцию (feedback_plan), что нужно найти на следующем шаге.\n"
        "   - Установи is_complete: false.\n\n"
        f"{integration_summary_prompt}\n\n"
        "Ответь СТРОГО валидным JSON:\n"
        '{"is_complete": true | false, "answer": "...", "feedback_plan": "..."}'
    )

    compose_messages: list[dict[str, str]] = [{"role": "system", "content": prompt}]
    compose_messages.extend(history[-settings.CONTEXT_ALWAYS_KEEP_LAST_MESSAGES:])
    compose_messages.append({"role": "user", "content": user_message})

    try:
        out = await llm_provider.chat_structured(
            messages=compose_messages,
            response_model=ReflexionComposeOutput,
            model=settings.LITELLM_PLANNER_MODEL or None,
            temperature=0.0,
            # Compose can legitimately produce long, structured answers.
            # Planner token budget is too small and may truncate JSON.
            max_tokens=max(int(settings.OLLAMA_NUM_PREDICT), int(settings.OLLAMA_NUM_PREDICT_PLANNER)),
        )
    except Exception as exc:
        err_text = str(exc or "")
        if "No valid JSON found in LLM response" in err_text:
            logger.info("Compose reflexion used fallback synthesis (non-JSON output)")
        else:
            logger.warning("Compose reflexion failed: %s", exc)
        recovered = extract_non_json_answer_from_exception(exc)
        if recovered:
            return {
                "final_answer": _sanitize_llm_answer(recovered),
                "is_complete": True,
                "feedback_plan": "",
                "iterations": iterations,
                "iteration": iterations,
            }
        # 1) If web context exists, run non-structured synthesis first.
        # Structured parse errors often contain a truncated preview (~200 chars).
        if web_fetch_content or web_search_results:
            web_context = web_fetch_content.strip()
            if not web_context and web_search_results:
                web_context = "\n".join(
                    f"- {web_result_field(r, 'title')}: {web_result_field(r, 'snippet')} ({web_result_field(r, 'url')})"
                    for r in web_search_results[:8]
                )
            try:
                fallback_answer = await llm_provider.chat(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Сформируй короткий и точный ответ пользователю только по данным ниже. "
                                "Если данных недостаточно, честно скажи, чего не хватает."
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"Вопрос: {user_message}\n\n"
                                f"Данные:\n{web_context[:12000]}"
                            ),
                        },
                    ],
                    temperature=0.0,
                    max_tokens=settings.OLLAMA_NUM_PREDICT,
                )
                fallback_answer = _sanitize_llm_answer(fallback_answer)
            except Exception:
                fallback_answer = existing_answer or build_raw_web_summary(
                    web_fetch_content=web_fetch_content,
                    web_search_results=web_search_results,
                    tool_results=tool_results,
                    sanitize_answer=_sanitize_llm_answer,
                )
        # 2) Recover plain answer embedded in structured-parse error text.
        else:
            fallback_answer = existing_answer or build_raw_tool_summary(tool_results)
        return {
            "final_answer": fallback_answer,
            "is_complete": True,
            "feedback_plan": "",
            "iterations": iterations,
            "iteration": iterations,
        }

    is_complete = bool(out.is_complete) or iterations >= max_iterations
    if is_complete:
        answer = _sanitize_llm_answer(
            out.answer
            or existing_answer
            or build_raw_web_summary(
                web_fetch_content=web_fetch_content,
                web_search_results=web_search_results,
                tool_results=tool_results,
                sanitize_answer=_sanitize_llm_answer,
            )
        )
        return {
            "final_answer": answer,
            "is_complete": True,
            "feedback_plan": "",
            "iterations": iterations,
            "iteration": iterations,
        }

    feedback_plan = str(out.feedback_plan or "").strip()
    if not feedback_plan:
        feedback_plan = "Нужен дополнительный поиск данных: уточнить недостающие факты и источники."

    return {
        "final_answer": "",
        "is_complete": False,
        "feedback_plan": feedback_plan,
        "iterations": iterations,
        "iteration": iterations,
    }


# ======================================================================
# Node: Output (guardrail + STM write)
# ======================================================================


async def output_node(state: dict) -> dict:
    """Final output processing: guardrail check + STM append."""
    from app.memory import memory_manager

    final_answer = state.get("final_answer", "")
    user_message = state.get("user_message", "")
    user_id = state.get("user_id")
    existing_calls = state.get("tool_calls_log") or []
    existing_artifacts = state.get("artifacts") or []

    final_answer, result = apply_output_guardrail(final_answer)

    export_kind = requested_export_kind(user_message)
    should_reenqueue = bool(settings.OUTPUT_ENABLE_AUTO_EXPORT_REENQUEUE) and should_reenqueue_export(
        export_kind=export_kind,
        existing_calls=existing_calls,
        existing_artifacts=existing_artifacts,
    )

    extra_calls = await enqueue_export_if_needed(
        user_id=user_id,
        final_answer=final_answer,
        export_kind=export_kind,
        should_reenqueue=should_reenqueue,
    )

    all_calls = [*existing_calls, *extra_calls]
    all_artifacts = [*existing_artifacts, *extract_artifacts(extra_calls)]

    final_answer, all_calls, all_artifacts = await apply_direct_route_fallback(
        user_id=user_id,
        user_message=user_message,
        final_answer=final_answer,
        all_calls=all_calls,
        all_artifacts=all_artifacts,
    )

    final_answer, all_calls, all_artifacts = await apply_inline_cron_bridge(
        user_id=user_id,
        user_message=user_message,
        final_answer=final_answer,
        all_calls=all_calls,
        all_artifacts=all_artifacts,
    )

    final_answer, all_calls, all_artifacts = await apply_inline_integration_bridge(
        user_id=user_id,
        user_message=user_message,
        final_answer=final_answer,
        all_calls=all_calls,
        all_artifacts=all_artifacts,
    )

    if extra_calls and export_kind and has_successful_export_call(extra_calls, export_kind):
        status_text = "PDF" if export_kind == "pdf" else "Excel"
        final_answer = (
            f"{final_answer}\n\n"
            f"{status_text} поставлен в очередь и будет отправлен отдельным сообщением."
        )

    final_answer = sanitize_false_attachment_claims(
        answer=final_answer,
        tool_calls=all_calls,
        artifacts=all_artifacts,
    )

    sanitized_final = _sanitize_llm_answer(final_answer)
    if sanitized_final != final_answer:
        _dev_log(
            "output_final_answer_sanitized",
            before_len=len(str(final_answer or "")),
            after_len=len(str(sanitized_final or "")),
        )
    final_answer = sanitized_final

    # Append to STM + extract facts to LTM
    if user_id and final_answer:
        try:
            await memory_manager.append_stm(user_id, user_message, final_answer)
        except Exception:
            logger.debug("STM append failed", exc_info=True)

        # Background LTM fact extraction (fire-and-forget)
        try:
            await extract_facts_to_ltm(user_id, user_message, final_answer)
        except Exception:
            logger.debug("LTM extraction in output_node failed", exc_info=True)

    return {
        "final_answer": final_answer,
        "output_guardrail": result,
        "tool_calls_log": all_calls,
        "artifacts": all_artifacts,
    }





