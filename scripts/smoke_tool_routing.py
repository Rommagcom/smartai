"""Smoke test: _parse_json robustness and _direct_route_from_message fallback.

Verifies that the tool-calling pipeline doesn't break on common LLM outputs
and that direct routing activates when the planner fails.
"""

import asyncio

from app.services.chat_service import chat_service
from app.services.tool_orchestrator_service import tool_orchestrator_service


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def test_parse_json_robustness() -> None:
    """_parse_json must extract JSON from various LLM output formats."""
    parse = tool_orchestrator_service._parse_json

    # Clean JSON
    result = parse('{"use_tools": true, "steps": [{"tool": "web_search", "arguments": {"query": "test"}}]}')
    ensure(result.get("use_tools") is True, f"clean JSON failed: {result}")

    # Markdown fenced
    result = parse('```json\n{"use_tools": true, "steps": []}\n```')
    ensure(isinstance(result, dict), f"fenced JSON failed: {result}")

    # Text before JSON (thinking output)
    result = parse(
        'Хорошо, пользователь хочет поиск. Вот план:\n'
        '{"use_tools": true, "steps": [{"tool": "web_search", "arguments": {"query": "test"}}]}'
    )
    ensure(result.get("use_tools") is True, f"text-before-JSON failed: {result}")

    # Backtick fence without json label
    result = parse('```\n{"use_tools": false, "steps": [], "response_hint": ""}\n```')
    ensure(result.get("use_tools") is False, f"fence-no-label failed: {result}")

    # Garbage input
    result = parse("I don't know how to help")
    ensure(result.get("use_tools") is False, f"garbage input should return use_tools=False: {result}")

    # JSON embedded in explanation
    result = parse(
        'Анализирую запрос...\n\n'
        '{"use_tools": true, "steps": [{"tool": "browser", "arguments": {"url": "https://example.com"}}], "response_hint": "open site"}\n\n'
        'Готово.'
    )
    ensure(result.get("use_tools") is True, f"embedded JSON failed: {result}")
    ensure(len(result.get("steps", [])) == 1, f"embedded JSON steps wrong: {result}")


def test_direct_route_from_message() -> None:
    """_direct_route_from_message must produce correct tool steps."""
    route = chat_service._direct_route_from_message

    # Explicit web_search
    steps = route("web_search smartcloud.kz")
    ensure(steps is not None, "web_search not matched")
    ensure(steps[0]["tool"] == "web_search", f"wrong tool: {steps}")
    ensure("smartcloud" in steps[0]["arguments"]["query"], f"wrong query: {steps}")

    # Explicit web_fetch
    steps = route("web_fetch https://example.com/api")
    ensure(steps is not None, "web_fetch not matched")
    ensure(steps[0]["tool"] == "web_fetch", f"wrong tool: {steps}")

    # browser with URL
    steps = route("browser https://smartcloud.kz")
    ensure(steps is not None, "browser not matched")
    ensure(steps[0]["tool"] == "browser", f"wrong tool: {steps}")

    # "открой сайт domain"
    steps = route("открой сайт smartcloud.kz")
    ensure(steps is not None, "open site not matched")
    ensure(any(s["tool"] == "browser" for s in steps), f"browser not in steps: {steps}")

    # memory_add
    steps = route("memory_add мой любимый цвет синий")
    ensure(steps is not None, "memory_add not matched")
    ensure(steps[0]["tool"] == "memory_add", f"wrong tool: {steps}")

    # "найди <query>"
    steps = route("найди последние новости о Казахстане")
    ensure(steps is not None, "search intent not matched")
    ensure(steps[0]["tool"] == "web_search", f"wrong tool: {steps}")

    # URL in message
    steps = route("посмотри что на https://example.com")
    ensure(steps is not None, "URL in message not matched")
    ensure(any(s["tool"] == "web_fetch" for s in steps), f"web_fetch not in steps: {steps}")

    # No tool intent
    steps = route("привет, как дела?")
    ensure(steps is None, f"should return None for casual message: {steps}")

    # Empty
    steps = route("")
    ensure(steps is None, f"should return None for empty: {steps}")


async def run() -> None:
    test_parse_json_robustness()
    test_direct_route_from_message()
    print("SMOKE_TOOL_ROUTING_OK")


if __name__ == "__main__":
    asyncio.run(run())
