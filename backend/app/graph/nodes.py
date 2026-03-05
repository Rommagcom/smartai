"""LangGraph agent nodes — each node is a pure function operating on AgentState.

Node architecture:
  ┌─────────┐
  │guardrail│ → input safety check
  ├─────────┤
  │ memory  │ → gather all memory layers
  ├─────────┤
  │ router  │ → decide: tool | chat | memory | clarify
  ├─────────┤
  │tool_exec│ → execute tool chain
  ├─────────┤
  │  chat   │ → generate final answer via LLM
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
# Node: Router (LLM-based intent classification)
# ======================================================================


async def router_node(state: dict) -> dict:
    """Classify user intent and decide the next step.

    Uses LiteLLM with structured output to guarantee valid RouterOutput.
    Falls back to deterministic pattern matching if LLM fails.
    """
    from app.llm import llm_provider
    from app.services.skills_registry_service import skills_registry_service

    user_message = state["user_message"]
    _dev_log("router_start", message_preview=user_message[:120])

    # 1. Try deterministic shortcuts first (fast path, no LLM call)
    deterministic = _deterministic_route(user_message)
    if deterministic is not None:
        _dev_log("router_deterministic", decision=deterministic.decision.value)
        return {
            "router_output": deterministic,
            "next_step": deterministic.decision.value,
        }

    # 2. LLM-based routing via structured output
    planner_model = settings.LITELLM_PLANNER_MODEL or None
    planner_prompt = (
        "Ты роутер AI-ассистента. Определи намерение пользователя.\n"
        "Верни JSON с полями: decision, steps, response_hint, confidence.\n"
        "decision: 'tool' — нужен инструмент, 'chat' — обычный разговор, "
        "'memory' — операция с памятью, 'clarify' — нужно уточнение.\n"
        "Доступные инструменты:\n"
        f"{skills_registry_service.planner_signatures()}\n"
        "Правила:\n"
        "1) Для напоминаний используй cron_add с schedule_text и task_text.\n"
        "2) Для PDF — pdf_create.\n"
        "3) Если просит подключить API — integration_add.\n"
        "4) Для удаления всех напоминаний — cron_delete_all.\n"
        "5) Не выдумывай аргументы.\n"
        "6) steps — максимум 3 шага.\n"
        "7) Для удаления факта: memory_search → memory_delete.\n"
    )

    try:
        router_output = await llm_provider.chat_structured(
            messages=[
                {"role": "system", "content": planner_prompt},
                {"role": "user", "content": user_message},
            ],
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
        logger.warning("Router LLM failed: %s, falling back to chat", exc)
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
    return {"final_answer": answer, "next_step": "output"}


# ======================================================================
# Node: Compose (tool results → final answer)
# ======================================================================


async def compose_node(state: dict) -> dict:
    """Compose a final answer from tool execution results."""
    from app.llm import llm_provider

    user_message = state["user_message"]
    system_prompt = state.get("system_prompt", "")
    tool_results: list[ToolResult] = state.get("tool_results", [])
    router_output: RouterOutput | None = state.get("router_output")
    response_hint = router_output.response_hint if router_output else ""

    # Check for deterministic answers first
    deterministic = _format_deterministic_tool_answer(tool_results)
    if deterministic:
        return {"final_answer": deterministic, "next_step": "output"}

    # All failed → honest error
    all_failed = all(not t.success for t in tool_results) if tool_results else True

    summary_prompt = (
        "Сформируй финальный ответ пользователю по результатам выполнения инструментов. "
    )
    if all_failed:
        summary_prompt += (
            "ВСЕ инструменты завершились с ошибкой. "
            "Объясни пользователю, что произошло, и предложи конкретный следующий шаг. "
            "НЕ притворяйся, что данные доступны."
        )
    else:
        summary_prompt += (
            "Если были ошибки, честно сообщи и предложи следующий шаг."
        )

    tool_calls_json = json.dumps(
        [t.model_dump() for t in tool_results],
        ensure_ascii=False,
        default=str,
    )[:16000]

    try:
        answer = await llm_provider.chat(
            messages=[
                {"role": "system", "content": f"{system_prompt}\n\n{summary_prompt}"},
                {
                    "role": "user",
                    "content": (
                        f"User message: {user_message}\n"
                        f"Response hint: {response_hint}\n"
                        f"Tool calls JSON: {tool_calls_json}"
                    ),
                },
            ],
            temperature=settings.LITELLM_TEMPERATURE,
        )
        answer = _sanitize_llm_answer(answer)
    except Exception as exc:
        logger.warning("Compose LLM failed: %s", exc)
        answer = _build_raw_tool_summary(tool_results)

    return {"final_answer": answer, "next_step": "output"}


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

    # Append to STM
    if user_id and final_answer:
        try:
            await memory_manager.append_stm(user_id, user_message, final_answer)
        except Exception:
            logger.debug("STM append failed", exc_info=True)

    return {
        "final_answer": final_answer,
        "output_guardrail": result,
    }


# ======================================================================
# Helper functions (shared across nodes)
# ======================================================================


import re


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
    cleaned = re.sub(r"<function_calls>[\s\S]*?</function_calls>", "", cleaned, re.IGNORECASE)
    cleaned = re.sub(r"<invoke[\s\S]*?</invoke>", "", cleaned, re.IGNORECASE)
    cleaned = cleaned.strip()
    return cleaned or "Не удалось сформировать ответ. Попробуйте уточнить запрос."


def _extract_artifacts(tool_calls: list[dict]) -> list[dict]:
    """Extract artifacts (PDF base64, etc.) from tool results."""
    artifacts = []
    for call in tool_calls:
        if not call.get("success"):
            continue
        result = call.get("result") if isinstance(call.get("result"), dict) else {}
        if result.get("file_base64"):
            artifacts.append({
                "type": "file",
                "filename": result.get("filename", "document.pdf"),
                "content_type": result.get("content_type", "application/pdf"),
                "base64": result["file_base64"],
            })
    return artifacts


def _format_deterministic_tool_answer(tool_results: list[ToolResult]) -> str | None:
    """Format known tool results without LLM."""
    for tr in tool_results:
        if not tr.success or not tr.result:
            continue
        if tr.tool == "cron_add":
            payload = tr.result.get("payload", {})
            if isinstance(payload, dict):
                task = payload.get("message", "")
                cron_expr = tr.result.get("cron_expression", "")
                if task:
                    return f"Напоминание создано: {task}" + (f" ({cron_expr})" if cron_expr else "")
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
            parts.append(str(msg)[:500])
        elif tr.result:
            parts.append(json.dumps(tr.result, ensure_ascii=False, default=str)[:500])

    if not parts:
        return "Не удалось получить данные. Повторите запрос позже."
    return "Результат:\n\n" + "\n\n".join(parts)
