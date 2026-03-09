import asyncio

from app.graph import nodes
from app.llm import llm_provider


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def run() -> None:
    original_chat_structured = llm_provider.chat_structured
    original_chat = llm_provider.chat

    try:
        # Case 1: structured parse error contains usable natural-language answer.
        async def broken_structured_with_text(*args, **kwargs):
            del args, kwargs
            raise ValueError(
                "No valid JSON found in LLM response: Сейчас в Алматы около +9C, пасмурно, влажность 57%."
            )

        async def unexpected_chat_call(*args, **kwargs):
            del args, kwargs
            return "UNEXPECTED_CHAT_CALL"

        llm_provider.chat_structured = broken_structured_with_text
        llm_provider.chat = unexpected_chat_call

        state = {
            "messages": ["Какая погода в Алматы"],
            "history_messages": [],
            "tool_results": [],
            "web_fetch_content": "Источник: +9C, пасмурно, влажность 57%",
            "web_search_results": [],
            "final_answer": "",
            "iterations": 0,
            "max_iterations": 2,
        }
        out = await nodes.compose_node(state)
        answer = str(out.get("final_answer") or "")
        ensure("Не удалось получить данные" not in answer, f"unexpected generic fallback: {answer}")
        ensure("Алматы" in answer or "+9" in answer, f"recovered answer mismatch: {answer}")

        # Case 2: structured parse error without embedded text -> fallback to llm_provider.chat.
        async def broken_structured_generic(*args, **kwargs):
            del args, kwargs
            raise RuntimeError("structured parser failed")

        async def fallback_chat(*args, **kwargs):
            del args, kwargs
            return "По данным источников: в Алматы около +9C, пасмурно, влажность 57%."

        llm_provider.chat_structured = broken_structured_generic
        llm_provider.chat = fallback_chat

        out2 = await nodes.compose_node(state)
        answer2 = str(out2.get("final_answer") or "")
        ensure("Не удалось получить данные" not in answer2, f"unexpected generic fallback #2: {answer2}")
        ensure("Алматы" in answer2 or "+9" in answer2, f"chat fallback answer mismatch: {answer2}")

        print("SMOKE_WEB_COMPOSE_FALLBACK_OK")
    finally:
        llm_provider.chat_structured = original_chat_structured
        llm_provider.chat = original_chat


if __name__ == "__main__":
    asyncio.run(run())
