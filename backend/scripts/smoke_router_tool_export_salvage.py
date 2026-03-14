import asyncio

from app.core.config import settings
from app.graph.nodes import router_node
from app.llm import llm_provider


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def run() -> None:
    original_chat_structured = llm_provider.chat_structured
    original_shortcuts = settings.ROUTER_ENABLE_DETERMINISTIC_SHORTCUTS
    original_fallbacks = settings.ROUTER_ENABLE_DETERMINISTIC_FALLBACKS
    original_salvage = settings.ROUTER_ENABLE_LLM_PARSE_SALVAGE

    async def broken_router_structured(*args, **kwargs):
        del args, kwargs
        await asyncio.sleep(0)
        # Simulate malformed/truncated tool JSON that appears in production logs.
        raise ValueError(
            "Failed to parse LLM response into RouterOutput after 2 attempts: "
            "Structured JSON payload not found in LLM response: "
            "{\"decision\":\"tool\",\"steps\":[{\"tool\":\"pdf_create\",\"arguments\":{\"title\":\"Рассказ\",\"content\":\"В небольшом городе"
        )

    try:
        llm_provider.chat_structured = broken_router_structured
        settings.ROUTER_ENABLE_DETERMINISTIC_SHORTCUTS = False
        settings.ROUTER_ENABLE_DETERMINISTIC_FALLBACKS = False
        settings.ROUTER_ENABLE_LLM_PARSE_SALVAGE = True

        state = {
            "user_message": "Напиши рассказ на страницу и сгенеоируй pdf документ",
            "user_id": None,
            "feedback_plan": "",
            "retrieved_tools": [],
            "history_messages": [],
        }
        out = await router_node(state)
        router_output = out.get("router_output")

        ensure(out.get("next_step") == "tool", f"expected tool routing, got: {out}")
        ensure(router_output is not None, f"router_output missing: {out}")
        steps = router_output.steps if hasattr(router_output, "steps") else []
        ensure(len(steps) == 1, f"unexpected steps: {steps}")
        ensure(str(steps[0].tool) == "pdf_create", f"unexpected tool: {steps[0].tool}")
        args = steps[0].arguments if isinstance(steps[0].arguments, dict) else {}
        ensure(bool(str(args.get("content") or "").strip()), f"missing recovered content: {args}")

        print("SMOKE_ROUTER_TOOL_EXPORT_SALVAGE_OK")
    finally:
        llm_provider.chat_structured = original_chat_structured
        settings.ROUTER_ENABLE_DETERMINISTIC_SHORTCUTS = original_shortcuts
        settings.ROUTER_ENABLE_DETERMINISTIC_FALLBACKS = original_fallbacks
        settings.ROUTER_ENABLE_LLM_PARSE_SALVAGE = original_salvage


if __name__ == "__main__":
    asyncio.run(run())
