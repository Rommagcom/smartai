import asyncio

from app.graph.routing_policy import followup_export_route


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def run() -> None:
    queued_history = [
        {"role": "user", "content": "Сделай PDF"},
        {"role": "assistant", "content": "Задача поставлена в очередь."},
    ]

    guarded = followup_export_route("Сохрани в PDF", queued_history)
    ensure(guarded is None, f"status-only assistant text must not be exported: {guarded}")

    content_history = [
        {"role": "user", "content": "Сделай план"},
        {
            "role": "assistant",
            "content": "Маркетинговый план:\n1. ICP\n2. Каналы\n3. KPI\nЕсли нужно, сохраню в PDF.",
        },
    ]

    valid = followup_export_route("Да, в PDF", content_history)
    ensure(valid is not None, "expected follow-up route for meaningful assistant content")
    ensure(valid.steps and valid.steps[0].tool == "pdf_create", f"unexpected route: {valid}")
    content = str(valid.steps[0].arguments.get("content") or "")
    ensure("Маркетинговый план" in content, f"expected plan content in export follow-up: {content}")
    ensure("очеред" not in content.lower(), f"queue status leaked into export content: {content}")

    print("SMOKE_EXPORT_FOLLOWUP_GUARD_OK")


if __name__ == "__main__":
    asyncio.run(run())
