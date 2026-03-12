from __future__ import annotations

from app.schemas.graph import RouterDecision, RouterOutput, ToolStep


def hard_structured_route(user_message: str) -> RouterOutput | None:
    """Route explicit command-style directives directly to tool execution."""
    from app.services.chat_service import ChatService

    steps = ChatService._direct_route_from_message(user_message)
    if not steps:
        return None

    tool_steps = [
        ToolStep(tool=step["tool"], arguments=step.get("arguments", {}))
        for step in steps
        if isinstance(step, dict) and step.get("tool")
    ]
    if not tool_steps:
        return None

    return RouterOutput(
        decision=RouterDecision.TOOL,
        steps=tool_steps,
        response_hint="Выполнить явную команду пользователя",
        confidence=0.99,
    )


def build_enriched_system_prompt(
    system_prompt: str,
    stm: list[str],
    ltm: list[str],
    rag: list[str],
    summary: str | None,
) -> str:
    """Enrich system prompt with memory context."""
    parts = [system_prompt]

    if summary:
        parts.append(f"\n\n{summary}")
    if ltm:
        parts.append("\n\nДолгосрочная память:\n" + "\n".join(f"- {item}" for item in ltm))
    if stm:
        parts.append("\n\nКонтекст текущей сессии:\n" + "\n".join(f"- {item}" for item in stm))
    if rag:
        parts.append("\n\nРелевантные документы:\n" + "\n".join(f"- {item}" for item in rag))

    return "\n".join(parts)
