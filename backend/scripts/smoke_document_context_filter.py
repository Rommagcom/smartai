from app.services.tool_orchestrator_service import ToolOrchestratorService


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run() -> None:
    # Control/admin step should never become a document data source.
    context_control_only = {
        "_steps": [
            {
                "tool": "cron_delete_all",
                "result": {
                    "status": "deleted_all",
                    "deleted_count": 3,
                    "message": "deleted",
                },
            }
        ]
    }
    bundle = ToolOrchestratorService._build_document_context_bundle(context_control_only)
    ensure(bundle is None, f"control-only bundle must be empty, got: {bundle}")

    # Data tool result should still be included.
    context_with_data = {
        "_steps": [
            {
                "tool": "cron_delete_all",
                "result": {"status": "deleted_all", "deleted_count": 3},
            },
            {
                "tool": "integration_call",
                "result": {
                    "status_code": 200,
                    "body": "Погода в Алматы: +12 C, облачно, ветер 3 м/с",
                },
            },
        ]
    }
    bundle2 = ToolOrchestratorService._build_document_context_bundle(context_with_data)
    ensure(isinstance(bundle2, dict), f"expected data bundle, got: {bundle2}")
    sources = bundle2.get("sources") if isinstance(bundle2.get("sources"), list) else []
    ensure(len(sources) == 1, f"expected one data source, got: {sources}")
    ensure(str(sources[0].get("tool") or "") == "integration_call", f"unexpected source tool: {sources}")

    print("SMOKE_DOCUMENT_CONTEXT_FILTER_OK")


if __name__ == "__main__":
    run()
