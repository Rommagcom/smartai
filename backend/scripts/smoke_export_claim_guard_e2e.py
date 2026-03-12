import asyncio

from app.graph.nodes import output_node
from scripts.smoke_env import apply_smoke_env_defaults, smoke_user_uuid

apply_smoke_env_defaults()


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def run() -> None:
    state = {
        "final_answer": "PDF-документ Almaty_Weather_Today.pdf успешно создан и готов к скачиванию.",
        "user_message": "Создай PDF документ с результатами прогноза погоды на сегодня в Алматы",
        "user_id": smoke_user_uuid(),
        "web_fetch_content": "",
        "web_search_results": [],
        "tool_calls_log": [
            {
                "tool": "pdf_create",
                "success": True,
                "result": {"status": "queued", "message": "📄 Документ готовится"},
            }
        ],
        "artifacts": [],
    }

    out = await output_node(state)
    answer = str(out.get("final_answer") or "")
    ensure("успешно создан" not in answer.lower(), f"false success claim leaked: {answer}")
    ensure("очеред" in answer.lower(), f"queue status missing: {answer}")
    ensure("вложени" in answer.lower(), f"missing attachment note: {answer}")

    # If artifact exists, answer should not be rewritten by this guard.
    state_with_artifact = dict(state)
    state_with_artifact["artifacts"] = [
        {
            "file_name": "Almaty_Weather_Today.pdf",
            "mime_type": "application/pdf",
            "file_base64": "Zm9v",
        }
    ]
    out2 = await output_node(state_with_artifact)
    answer2 = str(out2.get("final_answer") or "")
    ensure("успешно создан" in answer2.lower(), "answer unexpectedly changed when artifact exists")

    print("SMOKE_EXPORT_CLAIM_GUARD_E2E_OK")


if __name__ == "__main__":
    asyncio.run(run())
