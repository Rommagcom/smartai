from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel

from app.core.config import settings
from app.graph.node_helpers import (
    _looks_like_incomplete_markdown_answer,
    _sanitize_llm_answer,
    build_raw_tool_summary,
    build_raw_web_summary,
    extract_non_json_answer_from_exception,
    web_result_field,
)
from app.schemas.graph import ToolResult

logger = logging.getLogger(__name__)


def _dev_log(event: str, **ctx: Any) -> None:
    if not settings.DEV_VERBOSE_LOGGING:
        return
    logger.info(
        f"graph node: {event}",
        extra={"context": {"component": "langgraph", "event": event, **ctx}},
    )


class ReflexionComposeOutput(BaseModel):
    is_complete: bool
    answer: str = ""
    feedback_plan: str = ""


async def _recover_truncated_web_answer(
    *,
    llm_provider: Any,
    answer: str,
    user_message: str,
    web_fetch_content: str,
    web_search_results: list[dict],
    tool_results: list[ToolResult],
) -> str:
    sanitized = _sanitize_llm_answer(answer)
    if not (web_fetch_content or web_search_results):
        return sanitized
    if not _looks_like_incomplete_markdown_answer(sanitized):
        return sanitized

    _dev_log("compose_truncated_markdown_detected", answer_len=len(sanitized))

    web_context = web_fetch_content.strip()
    if not web_context and web_search_results:
        web_context = "\n".join(
            f"- {web_result_field(r, 'title')}: {web_result_field(r, 'snippet')} ({web_result_field(r, 'url')})"
            for r in web_search_results[:8]
        )

    try:
        recovered = await llm_provider.chat(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Сформируй полный ответ по данным ниже. "
                        "Пиши обычным текстом или коротким маркированным списком. "
                        "Не используй markdown-таблицы и не оставляй ответ незавершенным."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Вопрос: {user_message}\n\nДанные:\n{web_context[:12000]}",
                },
            ],
            temperature=0.0,
            max_tokens=settings.OLLAMA_NUM_PREDICT,
        )
        recovered = _sanitize_llm_answer(recovered)
        if recovered and not _looks_like_incomplete_markdown_answer(recovered):
            return recovered
    except Exception:
        logger.debug("truncated web answer recovery failed", exc_info=True)

    return build_raw_web_summary(
        web_fetch_content=web_fetch_content,
        web_search_results=web_search_results,
        tool_results=tool_results,
        sanitize_answer=_sanitize_llm_answer,
    )


def _extract_doc_ask_result(tool_results: list[ToolResult]) -> dict | None:
    return next(
        (
            tr.result
            for tr in tool_results
            if tr.success and str(tr.tool or "") == "doc_ask" and isinstance(tr.result, dict)
        ),
        None,
    )


def _build_doc_context(doc_items: Any) -> str:
    source_lines: list[str] = []
    if not isinstance(doc_items, list):
        return ""

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
    return "\n\n".join(source_lines).strip()


async def _compose_doc_ask_answer(
    *,
    llm_provider: Any,
    user_message: str,
    tool_results: list[ToolResult],
    iterations: int,
) -> dict | None:
    doc_ask_result = _extract_doc_ask_result(tool_results)
    if not isinstance(doc_ask_result, dict):
        return None

    doc_answer = str(doc_ask_result.get("answer") or "").strip()
    doc_context = _build_doc_context(doc_ask_result.get("items", []))
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
    return None


def _build_context_text(
    *,
    state_context: list[str],
    web_fetch_content: str,
    web_search_results: list[dict],
    tool_results: list[ToolResult],
) -> str:
    context_chunks = list(state_context)

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
        sanitized_results = []
        strip_keys = {"headers", "file_base64", "base64"}
        for tool_result in tool_results:
            dumped = tool_result.model_dump()
            result = dumped.get("result")
            if isinstance(result, dict):
                dumped["result"] = {k: v for k, v in result.items() if k not in strip_keys}
            sanitized_results.append(dumped)
        context_chunks.append(
            "Результаты инструментов:\n"
            + json.dumps(sanitized_results, ensure_ascii=False, default=str)[:16000]
        )

    return "\n\n".join([chunk for chunk in context_chunks if str(chunk).strip()])


def _build_compose_prompt(
    *,
    user_message: str,
    context_text: str,
    iterations: int,
    max_iterations: int,
    has_integration: bool,
    all_failed: bool,
    has_web_context: bool,
) -> str:
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

    web_formatting_prompt = ""
    if has_web_context:
        web_formatting_prompt = (
            "\n\nДОПОЛНИТЕЛЬНО ДЛЯ WEB-ОТВЕТОВ:\n"
            "Не используй markdown-таблицы. Предпочитай обычный текст и короткие списки. "
            "Ответ должен быть завершенным, без оборванных строк и незакрытого markdown."
        )

    return (
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
        f"{integration_summary_prompt}{web_formatting_prompt}\n\n"
        "Ответь СТРОГО валидным JSON:\n"
        '{"is_complete": true | false, "answer": "...", "feedback_plan": "..."}'
    )


def _build_web_context(web_fetch_content: str, web_search_results: list[dict], *, limit: int = 8) -> str:
    web_context = web_fetch_content.strip()
    if web_context or not web_search_results:
        return web_context
    return "\n".join(
        f"- {web_result_field(r, 'title')}: {web_result_field(r, 'snippet')} ({web_result_field(r, 'url')})"
        for r in web_search_results[:limit]
    )


def _build_complete_result(*, answer: str, iterations: int) -> dict:
    return {
        "final_answer": answer,
        "is_complete": True,
        "feedback_plan": "",
        "iterations": iterations,
        "iteration": iterations,
    }


def _build_incomplete_result(*, feedback_plan: str, iterations: int) -> dict:
    return {
        "final_answer": "",
        "is_complete": False,
        "feedback_plan": feedback_plan,
        "iterations": iterations,
        "iteration": iterations,
    }


def _should_use_recovered_answer(recovered: str, *, has_web_context: bool) -> bool:
    recovered = _sanitize_llm_answer(recovered)
    if not recovered:
        return False
    if not has_web_context:
        return True
    if _looks_like_incomplete_markdown_answer(recovered):
        return False
    return len(recovered) >= 40 or any(mark in recovered for mark in (".", "!", "?", "+"))


async def _synthesize_web_fallback(
    *,
    llm_provider: Any,
    user_message: str,
    web_fetch_content: str,
    web_search_results: list[dict],
    tool_results: list[ToolResult],
    existing_answer: str,
) -> str:
    web_context = _build_web_context(web_fetch_content, web_search_results)
    try:
        fallback_answer = await llm_provider.chat(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Сформируй короткий и точный ответ пользователю только по данным ниже. "
                        "Если данных недостаточно, честно скажи, чего не хватает. "
                        "Не используй markdown-таблицы."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Вопрос: {user_message}\n\nДанные:\n{web_context[:12000]}",
                },
            ],
            temperature=0.0,
            max_tokens=settings.OLLAMA_NUM_PREDICT,
        )
        return await _recover_truncated_web_answer(
            llm_provider=llm_provider,
            answer=fallback_answer,
            user_message=user_message,
            web_fetch_content=web_fetch_content,
            web_search_results=web_search_results,
            tool_results=tool_results,
        )
    except Exception:
        return existing_answer or build_raw_web_summary(
            web_fetch_content=web_fetch_content,
            web_search_results=web_search_results,
            tool_results=tool_results,
            sanitize_answer=_sanitize_llm_answer,
        )


async def _recover_compose_failure(
    *,
    llm_provider: Any,
    exc: Exception,
    user_message: str,
    existing_answer: str,
    iterations: int,
    web_fetch_content: str,
    web_search_results: list[dict],
    tool_results: list[ToolResult],
) -> dict:
    err_text = str(exc or "")
    if "No valid JSON found in LLM response" in err_text:
        logger.info("Compose reflexion used fallback synthesis (non-JSON output)")
    else:
        logger.warning("Compose reflexion failed: %s", exc)

    recovered = extract_non_json_answer_from_exception(exc)
    has_web_context = bool(web_fetch_content or web_search_results)
    if recovered and _should_use_recovered_answer(recovered, has_web_context=has_web_context):
        return _build_complete_result(answer=_sanitize_llm_answer(recovered), iterations=iterations)

    if has_web_context:
        fallback_answer = await _synthesize_web_fallback(
            llm_provider=llm_provider,
            user_message=user_message,
            web_fetch_content=web_fetch_content,
            web_search_results=web_search_results,
            tool_results=tool_results,
            existing_answer=existing_answer,
        )
    else:
        fallback_answer = existing_answer or build_raw_tool_summary(tool_results)

    return _build_complete_result(answer=fallback_answer, iterations=iterations)


def _extract_compose_inputs(state: dict) -> dict[str, Any]:
    messages_state: list[str] = state.get("messages") or []
    tool_results: list[ToolResult] = state.get("tool_results", [])
    return {
        "user_message": (messages_state[-1] if messages_state else state.get("user_message", "")).strip(),
        "history": state.get("history_messages") or [],
        "tool_results": tool_results,
        "web_fetch_content": state.get("web_fetch_content") or "",
        "web_search_results": state.get("web_search_results") or [],
        "existing_answer": str(state.get("final_answer") or "").strip(),
        "iterations": int((state.get("iterations") or state.get("iteration") or 0) + 1),
        "max_iterations": int(state.get("max_iterations") or settings.LANGGRAPH_MAX_ITERATIONS),
        "all_failed": all(not result.success for result in tool_results) if tool_results else True,
        "has_integration": any(str(result.tool or "") == "integration_call" for result in tool_results),
        "state_context": list(state.get("context") or []),
    }


def _try_existing_answer_short_circuit(inputs: dict[str, Any]) -> dict | None:
    if inputs["existing_answer"] and not inputs["tool_results"] and not inputs["web_search_results"] and not inputs["web_fetch_content"]:
        return _build_complete_result(answer=inputs["existing_answer"], iterations=inputs["iterations"])
    return None


def _build_compose_messages(*, prompt: str, history: list[dict], user_message: str) -> list[dict[str, str]]:
    compose_messages: list[dict[str, str]] = [{"role": "system", "content": prompt}]
    compose_messages.extend(history[-settings.CONTEXT_ALWAYS_KEEP_LAST_MESSAGES:])
    compose_messages.append({"role": "user", "content": user_message})
    return compose_messages


async def _finalize_complete_compose(
    *,
    llm_provider: Any,
    output: ReflexionComposeOutput,
    existing_answer: str,
    iterations: int,
    user_message: str,
    web_fetch_content: str,
    web_search_results: list[dict],
    tool_results: list[ToolResult],
) -> dict:
    answer = await _recover_truncated_web_answer(
        llm_provider=llm_provider,
        answer=(
            output.answer
            or existing_answer
            or build_raw_web_summary(
                web_fetch_content=web_fetch_content,
                web_search_results=web_search_results,
                tool_results=tool_results,
                sanitize_answer=_sanitize_llm_answer,
            )
        ),
        user_message=user_message,
        web_fetch_content=web_fetch_content,
        web_search_results=web_search_results,
        tool_results=tool_results,
    )
    return _build_complete_result(answer=answer, iterations=iterations)


async def compose_node(state: dict) -> dict:
    """Reflexion synth node: either finalize answer or return explicit feedback plan."""
    from app.llm import llm_provider

    inputs = _extract_compose_inputs(state)
    short_circuit = _try_existing_answer_short_circuit(inputs)
    if short_circuit is not None:
        return short_circuit

    doc_ask_answer = await _compose_doc_ask_answer(
        llm_provider=llm_provider,
        user_message=inputs["user_message"],
        tool_results=inputs["tool_results"],
        iterations=inputs["iterations"],
    )
    if doc_ask_answer is not None:
        return doc_ask_answer

    context_text = _build_context_text(
        state_context=inputs["state_context"],
        web_fetch_content=inputs["web_fetch_content"],
        web_search_results=inputs["web_search_results"],
        tool_results=inputs["tool_results"],
    )
    prompt = _build_compose_prompt(
        user_message=inputs["user_message"],
        context_text=context_text,
        iterations=inputs["iterations"],
        max_iterations=inputs["max_iterations"],
        has_integration=inputs["has_integration"],
        all_failed=inputs["all_failed"],
        has_web_context=bool(inputs["web_fetch_content"] or inputs["web_search_results"]),
    )

    compose_messages = _build_compose_messages(
        prompt=prompt,
        history=inputs["history"],
        user_message=inputs["user_message"],
    )

    try:
        output = await llm_provider.chat_structured(
            messages=compose_messages,
            response_model=ReflexionComposeOutput,
            model=settings.LITELLM_PLANNER_MODEL or None,
            temperature=0.0,
            max_tokens=max(int(settings.OLLAMA_NUM_PREDICT), int(settings.OLLAMA_NUM_PREDICT_PLANNER)),
        )
    except Exception as exc:
        return await _recover_compose_failure(
            llm_provider=llm_provider,
            exc=exc,
            user_message=inputs["user_message"],
            existing_answer=inputs["existing_answer"],
            iterations=inputs["iterations"],
            web_fetch_content=inputs["web_fetch_content"],
            web_search_results=inputs["web_search_results"],
            tool_results=inputs["tool_results"],
        )

    is_complete = bool(output.is_complete) or inputs["iterations"] >= inputs["max_iterations"]
    if is_complete:
        return await _finalize_complete_compose(
            llm_provider=llm_provider,
            output=output,
            existing_answer=inputs["existing_answer"],
            iterations=inputs["iterations"],
            user_message=inputs["user_message"],
            web_fetch_content=inputs["web_fetch_content"],
            web_search_results=inputs["web_search_results"],
            tool_results=inputs["tool_results"],
        )

    feedback_plan = str(output.feedback_plan or "").strip()
    if not feedback_plan:
        feedback_plan = "Нужен дополнительный поиск данных: уточнить недостающие факты и источники."

    return _build_incomplete_result(feedback_plan=feedback_plan, iterations=inputs["iterations"])