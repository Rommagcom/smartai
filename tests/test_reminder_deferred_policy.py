from __future__ import annotations

from types import SimpleNamespace

from search_agent.agent.graph import OllamaLangGraphAgent


class _FakeRegistry:
    def __init__(self, callable_map: dict[str, object]) -> None:
        self._callable_map = callable_map

    def get_callable_map(self) -> dict[str, object]:
        return dict(self._callable_map)


def _build_agent(callable_map: dict[str, object]) -> OllamaLangGraphAgent:
    agent = OllamaLangGraphAgent.__new__(OllamaLangGraphAgent)
    agent.settings = SimpleNamespace(enable_dynamic_tools=True, max_tool_result_chars=8000)
    agent.registry = _FakeRegistry(callable_map)
    return agent


def test_reminder_create_skips_non_reminder_tool_calls() -> None:
    call_log: list[str] = []

    def reminder_scheduler(**kwargs: object) -> str:
        del kwargs
        call_log.append("reminder_create")
        return '{"status":"created"}'

    def generate_pdf_document(**kwargs: object) -> str:
        del kwargs
        call_log.append("pdf")
        return '{"type":"file","filename":"x.pdf","base64":"AA=="}'

    agent = _build_agent(
        {
            "reminder_scheduler": reminder_scheduler,
            "generate_pdf_document": generate_pdf_document,
        }
    )

    state = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "reminder_scheduler",
                            "arguments": {
                                "action": "create",
                                "prompt": "Курсы валют и погода",
                                "once_at": "2026-03-20T06:25:00+00:00",
                            },
                        }
                    },
                    {
                        "function": {
                            "name": "generate_pdf_document",
                            "arguments": {"content": "short report"},
                        }
                    },
                ],
            }
        ],
        "step_count": 0,
        "token_usage": {},
        "chat_id": 100,
    }

    result = agent._tools_node(state)
    tool_messages = result["messages"]

    assert call_log == ["reminder_create"]
    assert len(tool_messages) == 2
    assert tool_messages[0]["tool_name"] == "reminder_scheduler"
    assert tool_messages[1]["tool_name"] == "generate_pdf_document"
    assert "Skipped deferred-policy execution" in tool_messages[1]["content"]


def test_reminder_non_create_does_not_skip_other_tools() -> None:
    call_log: list[str] = []

    def reminder_scheduler(**kwargs: object) -> str:
        del kwargs
        call_log.append("reminder_list")
        return '{"status":"ok"}'

    def generate_pdf_document(**kwargs: object) -> str:
        del kwargs
        call_log.append("pdf")
        return '{"type":"file","filename":"x.pdf","base64":"AA=="}'

    agent = _build_agent(
        {
            "reminder_scheduler": reminder_scheduler,
            "generate_pdf_document": generate_pdf_document,
        }
    )

    state = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "reminder_scheduler",
                            "arguments": {"action": "list"},
                        }
                    },
                    {
                        "function": {
                            "name": "generate_pdf_document",
                            "arguments": {"content": "short report"},
                        }
                    },
                ],
            }
        ],
        "step_count": 0,
        "token_usage": {},
        "chat_id": 100,
    }

    result = agent._tools_node(state)
    tool_messages = result["messages"]

    assert call_log == ["reminder_list", "pdf"]
    assert len(tool_messages) == 2
    assert "Skipped deferred-policy execution" not in tool_messages[1]["content"]
