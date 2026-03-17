import app.services.tool_orchestrator_service as tos
from app.services.tool_orchestrator_service import ToolOrchestratorService


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def run() -> None:
    original_expand = tos._expand_prompt_to_content

    async def fake_expand(prompt_text: str, title_hint: str = "") -> str:
        del title_hint
        return (
            "## Маркетинговый план\n"
            "- ЦА: малый и средний бизнес\n"
            "- Каналы: контент, партнерства, demo-воронка\n"
            f"Источник запроса: {prompt_text[:80]}"
        )

    try:
        tos._expand_prompt_to_content = fake_expand
        source = (
            "Разработай маркетинговый план по продаже услуги AI персональный ассистент "
            "как ты с твоим функционалом и сохрани в pdf"
        )
        out = await ToolOrchestratorService._maybe_summarize_content(
            source,
            title_hint="Маркетинговый план AI-персональный ассистент",
        )

        lowered = str(out).lower()
        ensure("## маркетинговый план" in lowered, f"expanded content missing: {out}")
        ensure(lowered != source.lower(), f"content was not expanded: {out}")

        print("SMOKE_PDF_PROMPT_EXPAND_EXPORT_REQUEST_OK")
    finally:
        tos._expand_prompt_to_content = original_expand


if __name__ == "__main__":
    import asyncio

    asyncio.run(run())
