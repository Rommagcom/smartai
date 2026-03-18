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
import logging
from typing import Any

from app.core.config import settings
from app.graph.compose_node import _recover_truncated_web_answer, compose_node
from app.graph.orchestration_types import GuardrailNodeUpdate, IntentLabel, NextStep, NodeUpdate
from app.graph.prompt_routing_policy import (
    build_enriched_system_prompt as _build_enriched_system_prompt,
)
from app.graph.text_policy import (
    looks_like_small_talk as _looks_like_small_talk,
    sanitize_llm_answer as _sanitize_llm_answer,
)
from app.graph.output_node import output_node
from app.graph.router_node import router_node
from app.graph.tool_execution_node import tool_execution_node
from app.schemas.graph import (
    GuardrailResult,
    GuardrailVerdict,
    RouterOutput,
)

logger = logging.getLogger(__name__)
_GENERIC_CHAT_FALLBACK_PREFIX = "Не удалось сформировать ответ."


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


def input_guardrail_node(state: dict) -> dict:
    """Check user input for safety issues before processing."""
    if not settings.GUARDRAILS_ENABLED:
        return GuardrailNodeUpdate(
            input_guardrail=GuardrailResult(verdict=GuardrailVerdict.PASS)
        ).to_state_update()

    user_message = state.get("user_message", "")

    # Length check
    if len(user_message) > settings.GUARDRAILS_MAX_INPUT_LENGTH:
        return GuardrailNodeUpdate(
            input_guardrail=GuardrailResult(
                verdict=GuardrailVerdict.BLOCK,
                reason=f"Message exceeds max length ({settings.GUARDRAILS_MAX_INPUT_LENGTH} chars)",
            ),
            final_answer="Сообщение слишком длинное. Пожалуйста, сократите запрос.",
            next_step=NextStep.END,
        ).to_state_update()

    # Prompt injection detection
    if settings.GUARDRAILS_BLOCK_PROMPT_INJECTION:
        from app.guardrails import prompt_shield
        result = prompt_shield.check_input(user_message)
        if result.verdict == GuardrailVerdict.BLOCK:
            return GuardrailNodeUpdate(
                input_guardrail=result,
                final_answer="Запрос отклонён системой безопасности.",
                next_step=NextStep.END,
            ).to_state_update()
        return GuardrailNodeUpdate(input_guardrail=result).to_state_update()

    return GuardrailNodeUpdate(
        input_guardrail=GuardrailResult(verdict=GuardrailVerdict.PASS)
    ).to_state_update()


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
        # Run context gathering and entity extraction in parallel — they are independent
        context, entities = await asyncio.gather(
            memory_manager.gather_context(
                db=db,
                user_id=user_id,
                session_id=session_id,
                user_message=user_message,
            ),
            memory_manager.extract_entities(user_message),
        )

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

    return NodeUpdate(
        values={
            "history_messages": context["history_messages"],
            "stm_context": context["stm_context"],
            "ltm_context": context["ltm_context"],
            "rag_context": context["rag_context"],
            "history_summary": context["history_summary"],
            "extracted_entities": entities,
        }
    ).to_state_update()


# ======================================================================
# Node: Intent Classifier (micro-router)
# ======================================================================


def intent_classifier_node(state: dict) -> dict:
    """Classify if request can skip heavy retrieval/tool routing.

    Uses only deterministic regex rules to avoid a redundant LLM round-trip.
    The router_node (which always runs for non-small_talk) has its own LLM-based
    intent classification, so a second LLM call here adds latency with no benefit.
    """
    messages: list[str] = state.get("messages") or []
    query = (messages[-1] if messages else state.get("user_message", "")).strip()
    if not query:
        return NodeUpdate(
            values={"intent": IntentLabel.SMALL_TALK},
            next_step=NextStep.CHAT,
        ).to_state_update()

    if _looks_like_small_talk(query):
        return NodeUpdate(
            values={"intent": IntentLabel.SMALL_TALK},
            next_step=NextStep.CHAT,
        ).to_state_update()

    return NodeUpdate(
        values={"intent": IntentLabel.NEEDS_TOOLS},
        next_step=NextStep.RETRIEVER,
    ).to_state_update()


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
        return NodeUpdate(values={"retrieved_tools": []}).to_state_update()

    _dev_log("retriever_start", user_id=str(user_id))

    try:
        hits = await vector_tool_registry.get_relevant_tools(
            user_query=user_message,
            user_id=user_id,
            top_k=settings.TOOL_RETRIEVER_TOP_K,
        )
        _dev_log("retriever_done", hits_count=len(hits))
        return NodeUpdate(values={"retrieved_tools": hits}).to_state_update()
    except Exception as exc:
        logger.warning("Tool retriever failed: %s", exc)
        return NodeUpdate(values={"retrieved_tools": []}).to_state_update()


# ======================================================================
# Node: Router / Planner (LLM-based intent classification)
# ======================================================================




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
        raw_answer = await llm_provider.chat(
            messages,
            temperature=settings.LITELLM_TEMPERATURE,
            max_tokens=settings.OLLAMA_NUM_PREDICT,
        )
        # Sanitize
        answer = _sanitize_llm_answer(raw_answer)
    except Exception as exc:
        logger.warning("Chat LLM failed: %s", exc)
        answer = (
            "Сервис генерации ответа сейчас временно недоступен. "
            "Повторите запрос через 10–30 секунд."
        )

    _dev_log("chat_done", answer_length=len(answer))
    return NodeUpdate(
        values={"final_answer": answer, "is_complete": True},
        next_step=NextStep.OUTPUT,
    ).to_state_update()


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

    return NodeUpdate(
        values={"web_search_results": results},
        next_step=NextStep.WEB_FETCH,
    ).to_state_update()


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
        return NodeUpdate(
            values={"web_fetch_content": ""},
            next_step=NextStep.COMPOSE,
        ).to_state_update()

    urls = [r["url"] for r in results[:_MAX_FETCH_PAGES] if r.get("url")]
    _dev_log("web_fetch_start", urls=urls)

    semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def _fetch_one(url: str, client: httpx.AsyncClient) -> str | None:
        async with semaphore:
            # Network timeouts are common for some sources, retry once before dropping.
            for attempt in range(2):
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
                    return None
                except httpx.TimeoutException as exc:
                    if attempt == 0:
                        await asyncio.sleep(0.2)
                        continue
                    logger.debug("web_fetch timeout for %s after retries: %s", url, str(exc))
                    return None
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code if exc.response is not None else "?"
                    logger.debug("web_fetch http status %s for %s", status, url)
                    return None
                except httpx.HTTPError as exc:
                    logger.debug("web_fetch transport error for %s: %s", url, str(exc))
                    return None
                except Exception:
                    logger.debug("web_fetch failed for %s", url, exc_info=True)
                    return None
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

    return NodeUpdate(
        values={"web_fetch_content": combined},
        next_step=NextStep.COMPOSE,
    ).to_state_update()


# ======================================================================
# Node: Compose (tool results → final answer)
# ======================================================================





