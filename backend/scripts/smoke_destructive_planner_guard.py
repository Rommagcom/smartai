from app.services.tool_orchestrator_service import ToolOrchestratorService


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run() -> None:
    plan = {
        "use_tools": True,
        "steps": [
            {"tool": "cron_delete_all", "arguments": {}},
            {"tool": "pdf_create", "arguments": {"title": "Погода", "content": "$all_sources"}},
        ],
        "response_hint": "",
    }

    guarded = ToolOrchestratorService._filter_destructive_steps_by_intent(
        plan=plan,
        user_message="Сделай PDF с погодой в Алматы",
    )
    steps = guarded.get("steps") if isinstance(guarded.get("steps"), list) else []
    ensure(len(steps) == 1, f"unexpected steps after guard: {steps}")
    ensure(str(steps[0].get("tool") or "") == "pdf_create", f"unexpected preserved step: {steps}")

    delete_plan = {
        "use_tools": True,
        "steps": [{"tool": "cron_delete_all", "arguments": {}}],
        "response_hint": "",
    }
    guarded_delete = ToolOrchestratorService._filter_destructive_steps_by_intent(
        plan=delete_plan,
        user_message="Удали все напоминания",
    )
    delete_steps = guarded_delete.get("steps") if isinstance(guarded_delete.get("steps"), list) else []
    ensure(len(delete_steps) == 1, f"delete intent must keep cron_delete_all: {delete_steps}")

    print("SMOKE_DESTRUCTIVE_PLANNER_GUARD_OK")


if __name__ == "__main__":
    run()
