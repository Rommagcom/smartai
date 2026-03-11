import asyncio

from app.core.config import settings
from app.graph.nodes import router_node
from app.schemas.graph import RouterDecision, RouterOutput
from app.llm import llm_provider


class _FakeClarifyResponse:
    decision = RouterDecision.CLARIFY
    steps = []
    response_hint = ""
    confidence = 0.97


class _FakeChatResponse:
    decision = RouterDecision.CHAT
    steps = []
    response_hint = ""
    confidence = 0.97


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def _run_case(fake_response_obj) -> None:
    original_chat_structured = llm_provider.chat_structured
    original_shortcuts = settings.ROUTER_ENABLE_DETERMINISTIC_SHORTCUTS
    original_fallbacks = settings.ROUTER_ENABLE_DETERMINISTIC_FALLBACKS
    original_override = settings.ROUTER_OVERRIDE_CLARIFY_LIVE_EXPORT

    async def _fake_chat_structured(*args, **kwargs):
        await asyncio.sleep(0)
        return fake_response_obj

    llm_provider.chat_structured = _fake_chat_structured  # type: ignore[assignment]
    settings.ROUTER_ENABLE_DETERMINISTIC_SHORTCUTS = False
    settings.ROUTER_ENABLE_DETERMINISTIC_FALLBACKS = True
    settings.ROUTER_OVERRIDE_CLARIFY_LIVE_EXPORT = True

    try:
        state = {
            "user_message": "Получи актуальный прогноз погоды и создай pdf документ",
            "user_id": None,
            "feedback_plan": "",
            "retrieved_tools": [],
            "history_messages": [],
        }
        out = await router_node(state)
    finally:
        llm_provider.chat_structured = original_chat_structured  # type: ignore[assignment]
        settings.ROUTER_ENABLE_DETERMINISTIC_SHORTCUTS = original_shortcuts
        settings.ROUTER_ENABLE_DETERMINISTIC_FALLBACKS = original_fallbacks
        settings.ROUTER_OVERRIDE_CLARIFY_LIVE_EXPORT = original_override

    ensure(out.get("next_step") == "tool", f"unexpected next_step: {out}")
    router_output = out.get("router_output")
    ensure(isinstance(router_output, RouterOutput), f"router_output type mismatch: {type(router_output)!r}")
    ensure(router_output.decision == RouterDecision.TOOL, f"unexpected decision: {router_output.decision}")
    ensure(len(router_output.steps) == 2, f"unexpected steps len: {len(router_output.steps)}")
    ensure(router_output.steps[0].tool == "web_search", f"unexpected first step: {router_output.steps[0].tool}")
    ensure(router_output.steps[1].tool == "pdf_create", f"unexpected second step: {router_output.steps[1].tool}")
    ensure(
        str(router_output.steps[1].arguments.get("content") or "") == "$prev.body",
        f"unexpected second step content arg: {router_output.steps[1].arguments}",
    )


async def run() -> None:
    await _run_case(_FakeClarifyResponse())
    await _run_case(_FakeChatResponse())
    print("SMOKE_ROUTER_CLARIFY_LIVE_EXPORT_OVERRIDE_OK")


if __name__ == "__main__":
    asyncio.run(run())
