import json

from app.services.tool_orchestrator_service import ToolOrchestratorService


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run() -> None:
    llm_text = "LLM_RESULT: курс валют на сегодня и краткий анализ"
    context = {
        "_steps": [
            {
                "tool": "integration_call",
                "result": {
                    "status": "ok",
                    "raw": llm_text,
                    "meta": {"source": "mock"},
                },
            },
            {
                "tool": "pdf_create",
                "result": {"status": "queued"},
            },
        ]
    }

    bundle = ToolOrchestratorService._build_document_context_bundle(context)
    ensure(isinstance(bundle, dict), f"bundle missing: {bundle}")
    sources = bundle.get("sources") if isinstance(bundle.get("sources"), list) else []
    ensure(len(sources) == 1, f"unexpected sources: {sources}")

    result = sources[0].get("result") if isinstance(sources[0], dict) else {}
    ensure(isinstance(result, dict), f"unexpected result payload: {result}")
    ensure("raw" in result, f"raw field was dropped: {json.dumps(result, ensure_ascii=False)}")
    ensure(llm_text in str(result.get("raw") or ""), "raw LLM text was not preserved")

    print("SMOKE_DOCUMENT_RAW_PASSTHROUGH_OK")


if __name__ == "__main__":
    run()
