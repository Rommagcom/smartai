import asyncio

from app.graph.nodes import compose_node
from app.llm import llm_provider
from app.schemas.graph import ToolResult


class _NeedMoreData:
    is_complete = False
    answer = ""
    feedback_plan = "Нужен источник с ценами конкурентов и каналами продаж."


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def run() -> None:
    original_chat_structured = llm_provider.chat_structured

    async def fake_chat_structured(*args, **kwargs):
        del args, kwargs
        await asyncio.sleep(0)
        return _NeedMoreData()

    llm_provider.chat_structured = fake_chat_structured
    try:
        state = {
            "user_message": "Сделай отчет и экспортируй в PDF",
            "history_messages": [],
            "tool_results": [
                ToolResult(
                    tool="pdf_create",
                    arguments={},
                    success=True,
                    result={"status": "queued", "message": "Задача поставлена в очередь."},
                    error=None,
                )
            ],
            "web_fetch_content": "",
            "web_search_results": [],
            "final_answer": "",
            "iterations": 0,
            "max_iterations": 5,
        }
        out = await compose_node(state)
    finally:
        llm_provider.chat_structured = original_chat_structured

    ensure(out.get("is_complete") is False, f"compose must keep insufficiency decision from LLM: {out}")
    feedback = str(out.get("feedback_plan") or "")
    ensure("конкурентов" in feedback.lower(), f"unexpected feedback_plan: {feedback}")

    print("SMOKE_COMPOSE_LLM_SUFFICIENCY_GATE_OK")


if __name__ == "__main__":
    asyncio.run(run())
