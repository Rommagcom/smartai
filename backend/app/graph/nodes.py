"""LangGraph agent nodes — each node is a pure function operating on AgentState.

Node architecture:
  ┌─────────┐
  │guardrail│ → input safety check
  ├─────────┤
  │ memory  │ → gather all memory layers
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
from typing import Any

from app.core.config import settings
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
    retrieved_tools: list[dict] = state.get("retrieved_tools") or []
    history: list[dict] = state.get("history_messages") or []
    _dev_log("router_start", message_preview=user_message[:120])

    # 1. Try deterministic shortcuts first (fast path, no LLM call)
    deterministic = _deterministic_route(user_message)
    if deterministic is not None:
        _dev_log("router_deterministic", decision=deterministic.decision.value)
        return {
            "router_output": deterministic,
            "next_step": deterministic.decision.value,
        }

    # 2. Load user integrations & dynamic tools for context
    integrations_block = ""
    dynamic_tools_block = ""
    if user_id:
        integrations_block, dynamic_tools_block = await _load_user_tool_context(user_id)

    # 3. Build retrieved-tools block from Milvus results
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

    # 4. LLM-based routing via structured output
    planner_model = settings.LITELLM_PLANNER_MODEL or None
    planner_prompt = (
        "Ты роутер AI-ассистента. Определи намерение пользователя.\n"
        "Верни JSON с полями: decision, steps, response_hint, confidence.\n"
        "decision: 'tool' — нужен инструмент, 'chat' — обычный разговор, "
        "'memory' — операция с памятью, 'clarify' — нужно уточнение, "
        "'web_search' — поиск информации в интернете.\n"
        "Доступные инструменты:\n"
        f"{skills_registry_service.planner_signatures()}\n"
        f"{dynamic_tools_block}"
        f"{integrations_block}"
        f"{retrieved_block}"
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
        return {
            "router_output": router_output,
            "next_step": router_output.decision.value,
        }
    except Exception as exc:
        logger.warning("Router LLM failed: %s, using fallback routing", exc)
        # If the message clearly asks for web search, don't lose the intent
        if _is_web_search_intent(user_message):
            query = _WEB_SEARCH_RE.sub("", user_message).strip() or user_message
            _dev_log("router_fallback_web_search", query=query[:120])
            fallback = RouterOutput(
                decision=RouterDecision.WEB_SEARCH,
                steps=[ToolStep(tool="web_search", arguments={"query": query})],
                response_hint="Выполни поиск в интернете",
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

        raw_results = await tool_orchestrator_service.execute_tool_chain(
            db=db,
            user=user,
            steps=steps_dicts,
            max_steps=settings.LANGGRAPH_MAX_ITERATIONS,
        )
        await db.commit()

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
    artifacts = _extract_artifacts(raw_results)

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

    parts: list[str] = []
    async with httpx.AsyncClient(
        timeout=_FETCH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; SmartAiBot/1.0)"},
    ) as client:
        for url in urls:
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
                    parts.append(f"### {url}\n{snippet}")
            except Exception:
                logger.debug("web_fetch failed for %s", url, exc_info=True)

    combined = "\n\n".join(parts) if parts else ""
    _dev_log("web_fetch_done", pages_ok=len(parts), total_len=len(combined))

    return {"web_fetch_content": combined, "next_step": "compose"}


# ======================================================================
# Node: Compose (tool results → final answer)
# ======================================================================


async def compose_node(state: dict) -> dict:
    """Compose a final answer from tool execution results or web search content."""
    from app.llm import llm_provider

    user_message = state["user_message"]
    system_prompt = state.get("system_prompt", "")
    history: list[dict] = state.get("history_messages") or []
    stm: list[str] = state.get("stm_context") or []
    ltm: list[str] = state.get("ltm_context") or []
    rag: list[str] = state.get("rag_context") or []
    summary: str | None = state.get("history_summary")
    tool_results: list[ToolResult] = state.get("tool_results", [])
    router_output: RouterOutput | None = state.get("router_output")
    response_hint = router_output.response_hint if router_output else ""
    web_fetch_content: str = state.get("web_fetch_content") or ""
    web_search_results: list[dict] = state.get("web_search_results") or []
    iteration = (state.get("iteration") or 0) + 1

    _completeness_suffix = (
        "\n\nВАЖНО: в конце ответа добавь строку-маркер:\n"
        "- Если данных достаточно для полного ответа: COMPLETENESS: COMPLETE\n"
        "- Если данных не хватает и нужен дополнительный поиск: COMPLETENESS: INCOMPLETE\n"
    )

    # ---- Web search graph path (no tool_results, web content available) ----
    if web_search_results and not tool_results:
        if web_fetch_content:
            web_prompt = (
                "Ты получил результаты поиска в интернете и извлечённый текст страниц. "
                "Проанализируй данные и сформируй ПОЛЕЗНЫЙ и ИНФОРМАТИВНЫЙ ответ. "
                "Указывай источники (ссылки) для ключевых фактов. "
                "Текст со страниц:\n" + web_fetch_content[:12000]
                + _completeness_suffix
            )
        else:
            snippets = "\n".join(
                f"- {r.get('title','')}: {r.get('snippet','')}\n  {r.get('url','')}"
                for r in web_search_results[:5]
            )
            web_prompt = (
                "Ты получил результаты поиска в интернете, "
                "но не удалось извлечь текст страниц. Используй сниппеты:\n"
                + snippets + "\n"
                "Сформируй максимально полный ответ по имеющимся данным. "
                "Указывай источники."
                + _completeness_suffix
            )

        enriched_system = _build_enriched_system_prompt(
            system_prompt=f"{system_prompt}\n\n{web_prompt}",
            stm=stm, ltm=ltm, rag=rag, summary=summary,
        )
        compose_messages: list[dict[str, str]] = [{"role": "system", "content": enriched_system}]
        compose_messages.extend(history[-settings.CONTEXT_ALWAYS_KEEP_LAST_MESSAGES:])
        compose_messages.append({"role": "user", "content": user_message})

        try:
            answer = await llm_provider.chat(
                messages=compose_messages,
                temperature=settings.LITELLM_TEMPERATURE,
            )
            is_complete = _extract_completeness(answer)
            answer = _strip_completeness_marker(answer)
            answer = _sanitize_llm_answer(answer)
        except Exception as exc:
            logger.warning("Compose (web) LLM failed: %s", exc)
            answer = "Не удалось сформировать ответ по результатам поиска."
            is_complete = True

        return {
            "final_answer": answer,
            "is_complete": is_complete,
            "iteration": iteration,
        }

    # ---- Standard tool results path ----

    # Check for deterministic answers first
    deterministic = _format_deterministic_tool_answer(tool_results)
    if deterministic:
        return {"final_answer": deterministic, "is_complete": True, "iteration": iteration}

    # Detect integration_call in results for specialised LLM formatting
    has_integration = any(
        t.tool == "integration_call" and t.success and t.result
        for t in tool_results
    )

    # Detect artifact tools (pdf_create, excel_create) in results
    has_artifact = any(
        t.tool in ("pdf_create", "excel_create") and t.success and t.result
        for t in tool_results
    )

    # All failed → honest error
    all_failed = all(not t.success for t in tool_results) if tool_results else True

    if has_integration and has_artifact and not all_failed:
        # Mixed chain: data → artifact — summarize data AND mention the document
        summary_prompt = (
            "Пользователь попросил получить данные и создать документ. "
            "Ты получил данные от внешнего API или из интернета, и документ был создан. "
            "Сформируй краткий ЧЕЛОВЕКОЧИТАЕМЫЙ отчёт по полученным данным. "
            "Упомяни, что документ с отчётом также создан и прикреплён. "
            "НЕ выводи сырой JSON/XML. Извлеки ключевые значения."
        )
    elif has_integration and not all_failed:
        summary_prompt = (
            "Ты получил ответ от внешнего API (интеграции). "
            "Проанализируй тело ответа и сформируй ЧЕЛОВЕКОЧИТАЕМЫЙ ответ. "
            "Если данные в XML/JSON — извлеки ключевые значения и представь "
            "в удобном виде (таблица, список, текст). "
            "НЕ выводи сырой XML/JSON. НЕ обрезай данные — покажи ВСЕ основные записи. "
            "Если пользователь просил конкретные данные — выдели их."
        )
    elif all_failed:
        summary_prompt = (
            "Сформируй финальный ответ пользователю по результатам выполнения инструментов. "
            "ВСЕ инструменты завершились с ошибкой. "
            "Объясни пользователю, что произошло, и предложи конкретный следующий шаг. "
            "НЕ притворяйся, что данные доступны и не придумывай результаты. Будь честным и конкретным."
        )
    else:
        summary_prompt = (
            "Сформируй финальный ответ пользователю по результатам выполнения инструментов. "
            "Если были ошибки, честно сообщи и предложи следующий шаг."
        )

    # Strip bulky fields (HTTP headers, file base64) from tool results before LLM
    sanitised_results = []
    _strip_keys = {"headers", "file_base64", "base64"}
    for t in tool_results:
        d = t.model_dump()
        res = d.get("result")
        if isinstance(res, dict):
            d["result"] = {k: v for k, v in res.items() if k not in _strip_keys}
        sanitised_results.append(d)

    tool_calls_json = json.dumps(
        sanitised_results,
        ensure_ascii=False,
        default=str,
    )[:16000]

    # Build context-enriched system prompt so compose sees conversation history
    enriched_system = _build_enriched_system_prompt(
        system_prompt=f"{system_prompt}\n\n{summary_prompt}" + _completeness_suffix,
        stm=stm,
        ltm=ltm,
        rag=rag,
        summary=summary,
    )

    compose_messages: list[dict[str, str]] = [{"role": "system", "content": enriched_system}]
    compose_messages.extend(history[-settings.CONTEXT_ALWAYS_KEEP_LAST_MESSAGES:])
    compose_messages.append({
        "role": "user",
        "content": (
            f"User message: {user_message}\n"
            f"Response hint: {response_hint}\n"
            f"Tool calls JSON: {tool_calls_json}"
        ),
    })

    try:
        answer = await llm_provider.chat(
            messages=compose_messages,
            temperature=settings.LITELLM_TEMPERATURE,
        )
        is_complete = _extract_completeness(answer)
        answer = _strip_completeness_marker(answer)
        answer = _sanitize_llm_answer(answer)
    except Exception as exc:
        logger.warning("Compose LLM failed: %s", exc)
        answer = _build_raw_tool_summary(tool_results)
        is_complete = True

    return {"final_answer": answer, "is_complete": is_complete, "iteration": iteration}


# ======================================================================
# Node: Output (guardrail + STM write)
# ======================================================================


async def output_node(state: dict) -> dict:
    """Final output processing: guardrail check + STM append."""
    from app.memory import memory_manager

    final_answer = state.get("final_answer", "")
    user_message = state.get("user_message", "")
    user_id = state.get("user_id")

    # Output guardrail
    if settings.GUARDRAILS_ENABLED:
        from app.guardrails import prompt_shield
        result = prompt_shield.check_output(final_answer)
        if result.verdict == GuardrailVerdict.BLOCK:
            final_answer = "Ответ заблокирован системой безопасности."
        elif result.modified_text:
            final_answer = result.modified_text
    else:
        result = GuardrailResult(verdict=GuardrailVerdict.PASS)

    # Append to STM + extract facts to LTM
    if user_id and final_answer:
        try:
            await memory_manager.append_stm(user_id, user_message, final_answer)
        except Exception:
            logger.debug("STM append failed", exc_info=True)

        # Background LTM fact extraction (fire-and-forget)
        try:
            await _extract_facts_to_ltm(user_id, user_message, final_answer)
        except Exception:
            logger.debug("LTM extraction in output_node failed", exc_info=True)

    return {
        "final_answer": final_answer,
        "output_guardrail": result,
    }


# ======================================================================
# Helper functions (shared across nodes)
# ======================================================================


import re


async def _extract_facts_to_ltm(user_id, user_message: str, assistant_response: str) -> None:
    """Background LTM fact extraction — runs after each response."""
    try:
        from app.db.session import AsyncSessionLocal
        from app.services.memory_service import memory_service

        async with AsyncSessionLocal() as db:
            await asyncio.wait_for(
                memory_service.extract_and_store_facts(
                    db, user_id, user_message, assistant_response
                ),
                timeout=15,
            )
            await db.commit()
    except Exception as exc:
        logger.debug("LTM fact extraction failed: %s", exc)


async def _load_user_tool_context(user_id) -> tuple[str, str]:
    """Load user integrations and dynamic tools for router context.

    Returns (integrations_block, dynamic_tools_block) as prompt fragments.
    """
    from app.db.session import AsyncSessionLocal
    from app.models.api_integration import ApiIntegration
    from app.services.dynamic_tool_service import dynamic_tool_service
    from sqlalchemy import select

    integrations_block = ""
    dynamic_tools_block = ""

    try:
        async with AsyncSessionLocal() as db:
            # Load user integrations
            result = await db.execute(
                select(ApiIntegration).where(
                    ApiIntegration.user_id == user_id,
                    ApiIntegration.is_active.is_(True),
                )
            )
            integrations = result.scalars().all()
            if integrations:
                lines: list[str] = []
                for integ in integrations:
                    eps = []
                    for ep in (integ.endpoints or []):
                        if isinstance(ep, dict) and ep.get("url"):
                            eps.append(str(ep["url"]))
                    ep_info = f" (endpoints: {', '.join(eps[:3])})" if eps else ""
                    lines.append(f"  - {integ.service_name}{ep_info}")
                integrations_block = (
                    "\nПодключённые интеграции пользователя "
                    "(вызывай через integration_call с service_name):\n"
                    + "\n".join(lines) + "\n"
                )

            # Load dynamic tools
            try:
                dynamic_sigs = await dynamic_tool_service.get_tools_for_planner(db, user_id)
                if dynamic_sigs:
                    dynamic_tools_block = (
                        f"\nПользовательские API-инструменты (динамические): {dynamic_sigs}. "
                        "Вызывай их точно по имени с префиксом dyn: (например dyn:weather_api).\n"
                    )
            except Exception as exc:
                logger.debug("failed to load dynamic tools for planner: %s", exc)
    except Exception as exc:
        logger.debug("failed to load user tool context: %s", exc)

    return integrations_block, dynamic_tools_block


# Regex for deterministic web search detection — fast path, no LLM needed
_WEB_SEARCH_RE = re.compile(
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


def _is_web_search_intent(user_message: str) -> bool:
    """Check if user message clearly asks for a web search."""
    return bool(_WEB_SEARCH_RE.search(user_message or ""))


def _deterministic_route(user_message: str) -> RouterOutput | None:
    """Pattern-match deterministic tool routes without LLM."""
    from app.services.chat_service import ChatService

    steps = ChatService._deterministic_tool_steps(user_message)
    if steps:
        tool_steps = [
            ToolStep(tool=s["tool"], arguments=s.get("arguments", {}))
            for s in steps
        ]
        return RouterOutput(
            decision=RouterDecision.TOOL,
            steps=tool_steps,
            confidence=0.95,
        )

    # Deterministic web search: catch obvious "search the web" patterns
    if _is_web_search_intent(user_message):
        # Extract search query by stripping the web-search prefix phrases
        query = _WEB_SEARCH_RE.sub("", user_message).strip()
        if not query:
            query = user_message
        _dev_log("deterministic_web_search", query=query[:120])
        return RouterOutput(
            decision=RouterDecision.WEB_SEARCH,
            steps=[ToolStep(tool="web_search", arguments={"query": query})],
            response_hint="Выполни поиск в интернете и представь результаты",
            confidence=0.95,
        )

    return None


def _build_enriched_system_prompt(
    system_prompt: str,
    stm: list[str],
    ltm: list[str],
    rag: list[str],
    summary: str | None,
) -> str:
    """Enrich system prompt with memory context."""
    parts = [system_prompt]

    if summary:
        parts.append(f"\n\n{summary}")
    if ltm:
        parts.append("\n\nДолгосрочная память:\n" + "\n".join(f"- {m}" for m in ltm))
    if stm:
        parts.append("\n\nКонтекст текущей сессии:\n" + "\n".join(f"- {s}" for s in stm))
    if rag:
        parts.append("\n\nРелевантные документы:\n" + "\n".join(f"- {r}" for r in rag))

    return "\n".join(parts)


def _sanitize_llm_answer(text: str) -> str:
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

    # Aggressive strip removed all content — fallback: strip only the tags themselves
    logger.debug("_sanitize_llm_answer: tag strip left empty, original %d chars", len(original))
    fallback = re.sub(r"</?(?:function_calls|invoke)[^>]*>", "", original, flags=re.IGNORECASE).strip()
    return fallback or "Не удалось сформировать ответ. Попробуйте уточнить запрос."


_COMPLETENESS_RE = re.compile(r"COMPLETENESS:\s*(COMPLETE|INCOMPLETE)", re.IGNORECASE)


def _extract_completeness(text: str) -> bool:
    """Extract the COMPLETENESS marker from LLM response. Defaults to True."""
    m = _COMPLETENESS_RE.search(text or "")
    if m:
        return m.group(1).upper() == "COMPLETE"
    return True


def _strip_completeness_marker(text: str) -> str:
    """Remove the COMPLETENESS: ... marker line from LLM output."""
    return _COMPLETENESS_RE.sub("", text or "").strip()


def _extract_artifacts(tool_calls: list[dict]) -> list[dict]:
    """Extract artifacts (PDF base64, etc.) from tool results."""
    artifacts = []
    for call in tool_calls:
        if not call.get("success"):
            continue
        result = call.get("result") if isinstance(call.get("result"), dict) else {}
        if result.get("file_base64"):
            artifacts.append({
                "file_name": result.get("file_name", "artifact.bin"),
                "mime_type": result.get("mime_type", "application/octet-stream"),
                "file_base64": result["file_base64"],
            })
    return artifacts


def _format_deterministic_tool_answer(tool_results: list[ToolResult]) -> str | None:
    """Format known tool results without LLM."""
    # Mixed chains (data-fetching + artifact-creation) → always use LLM compose
    _data_tools = {"integration_call", "dynamic_tool_call"}
    _artifact_tools = {"pdf_create", "excel_create"}
    tools_in_chain = {tr.tool for tr in tool_results if tr.success}
    if tools_in_chain & _data_tools and tools_in_chain & _artifact_tools:
        return None

    for tr in tool_results:
        if not tr.success or not tr.result:
            continue
        if tr.tool == "pdf_create":
            fname = tr.result.get("file_name") or "document.pdf"
            size = tr.result.get("size_bytes") or 0
            size_kb = f" ({size / 1024:.1f} KB)" if size else ""
            return f"Документ {fname} создан{size_kb}."
        if tr.tool == "excel_create":
            fname = tr.result.get("file_name") or "document.xlsx"
            size = tr.result.get("size_bytes") or 0
            size_kb = f" ({size / 1024:.1f} KB)" if size else ""
            return f"Документ {fname} создан{size_kb}."
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
        # Dynamic Tool Injection responses
        if tr.tool == "dynamic_tool_register":
            msg = tr.result.get("message", "")
            if msg:
                return msg
        # Register API Tool (with Milvus)
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
            return "Пользовательский API-инструмент удалён."
        if tr.tool == "dynamic_tool_delete_all":
            count = tr.result.get("deleted_count", 0)
            return f"Все пользовательские API-инструменты удалены ({count})."
        if tr.tool == "dynamic_tool_call" or str(tr.tool).startswith("dyn:") or str(tr.tool).startswith("dyn_"):
            # Let compose_node handle rich formatting via LLM
            pass
        if tr.tool == "integration_call":
            status_code = int(tr.result.get("status_code") or 0)
            body = str(tr.result.get("body") or "").strip()
            if status_code == 0 and not body:
                return "Запрос к интеграции не вернул данных."
            if status_code >= 400:
                preview = body[:2000] if body else ""
                return f"Запрос к интеграции вернул ошибку (HTTP {status_code}).\n{preview}".strip()
            if not body:
                return f"Ответ интеграции (HTTP {status_code}): пустое тело."
            # Let compose_node format with LLM for rich responses
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


def _build_raw_tool_summary(tool_results: list[ToolResult]) -> str:
    """Last-resort summary from raw tool output."""
    parts: list[str] = []
    for tr in tool_results:
        if not tr.success or not tr.result:
            continue
        msg = tr.result.get("message", "")
        if msg:
            parts.append(str(msg)[:8000])
        elif tr.result:
            # Strip headers from HTTP responses to save space for body
            dump_data = tr.result
            if "body" in tr.result and "headers" in tr.result:
                dump_data = {k: v for k, v in tr.result.items() if k != "headers"}
            parts.append(json.dumps(dump_data, ensure_ascii=False, default=str)[:8000])

    if not parts:
        return "Не удалось получить данные. Повторите запрос позже."
    return "Результат:\n\n" + "\n\n".join(parts)
