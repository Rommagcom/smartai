from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph
from langsmith import traceable
from ollama import Client

from search_agent.agent.prompts import SYSTEM_PROMPT
from search_agent.agent.ollama_tools import DOCUMENT_RAG_TOOL_SCHEMA, document_rag, web_search, web_fetch
from search_agent.config import Settings
from search_agent.dynamic_skills.registry import DynamicToolRegistry


_MAX_INPUT_CHARS = 8000
_MAX_TOOL_CONTENT_CHARS_FOR_MODEL = 12000
_PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior)\s+instructions", re.IGNORECASE),
    re.compile(r"(system|developer)\s+prompt", re.IGNORECASE),
    re.compile(r"reveal\s+(your\s+)?(instructions|prompt)", re.IGNORECASE),
    re.compile(r"jailbreak", re.IGNORECASE),
]


logger = logging.getLogger(__name__)


class AgentState(TypedDict):
    messages: Annotated[list[dict[str, Any]], list.__add__]
    step_count: int
    token_usage: dict[str, int]
    chat_id: int | None
    org_id: str
    team_id: str
    user_id: int
    role: str
    allowed_dynamic_tools: list[str]


@dataclass(slots=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    request_count: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "request_count": self.request_count,
        }

    @staticmethod
    def from_dict(value: dict[str, int] | None) -> "TokenUsage":
        value = value or {}
        return TokenUsage(
            prompt_tokens=int(value.get("prompt_tokens", 0)),
            completion_tokens=int(value.get("completion_tokens", 0)),
            total_tokens=int(value.get("total_tokens", 0)),
            request_count=int(value.get("request_count", 0)),
        )


@dataclass(slots=True)
class AgentRunResult:
    answer: str
    thinking: str | None
    messages: list[dict[str, Any]]
    token_usage: TokenUsage


class OllamaLangGraphAgent:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.registry = DynamicToolRegistry(settings.dynamic_skills_dir)
        
        # Initialize Ollama client with API key authentication
        api_key = os.getenv("OLLAMA_API_KEY", "").strip()
        headers = {
            "Authorization": f"Bearer {api_key}"
        } if api_key else {}
        
        ollama_url = settings.ollama_base_url or "http://localhost:11434"
        self.client = Client(host=ollama_url, headers=headers)
        self._graph = self._build_graph()

    def refresh_dynamic_tools(self) -> None:
        self.registry.refresh()

    @traceable(name="ollama_langgraph_agent_run")
    def run(
        self,
        user_message: str,
        history_messages: list[dict[str, Any]] | None = None,
        chat_id: int | None = None,
        org_id: str = "default-org",
        team_id: str = "chat",
        user_id: int = 0,
        role: str = "member",
        allowed_dynamic_tools: set[str] | None = None,
    ) -> AgentRunResult:
        if self.settings.enable_dynamic_tools:
            self.refresh_dynamic_tools()
        self._configure_langsmith_env()

        initial_messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]
        if history_messages:
            initial_messages.extend(history_messages)
        initial_messages.append({"role": "user", "content": user_message})

        result_state = self._graph.invoke(
            {
                "messages": initial_messages,
                "step_count": 0,
                "token_usage": TokenUsage().to_dict(),
                "chat_id": chat_id,
                "org_id": org_id,
                "team_id": team_id,
                "user_id": int(user_id),
                "role": role,
                "allowed_dynamic_tools": sorted(allowed_dynamic_tools or set()),
            }
        )
        messages = result_state["messages"]
        last_assistant = self._last_assistant_message(messages)
        usage = TokenUsage.from_dict(result_state.get("token_usage"))

        return AgentRunResult(
            answer=str(last_assistant.get("content") or ""),
            thinking=last_assistant.get("thinking"),
            messages=messages,
            token_usage=usage,
        )

    def _configure_langsmith_env(self) -> None:
        if not self.settings.langsmith_tracing:
            return
        os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
        os.environ.setdefault("LANGSMITH_PROJECT", self.settings.langsmith_project)

    def _build_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node("guardrail", self._guardrail_node)
        graph.add_node("agent", self._agent_node)
        graph.add_node("tools", self._tools_node)
        graph.set_entry_point("guardrail")
        graph.add_conditional_edges("guardrail", self._route_after_guardrail, {"agent": "agent", "end": END})
        graph.add_conditional_edges("agent", self._route_after_agent, {"tools": "tools", "end": END})
        graph.add_edge("tools", "agent")
        return graph.compile()

    def _guardrail_node(self, state: AgentState) -> dict[str, Any]:
        messages = state.get("messages") or []
        user_message = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                user_message = str(msg.get("content") or "")
                break

        if len(user_message) > _MAX_INPUT_CHARS:
            return {
                "messages": [
                    {
                        "role": "assistant",
                        "content": "Запрос слишком длинный. Пожалуйста, сократите текст и попробуйте снова.",
                    }
                ]
            }

        for pattern in _PROMPT_INJECTION_PATTERNS:
            if pattern.search(user_message):
                return {
                    "messages": [
                        {
                            "role": "assistant",
                            "content": "Запрос отклонен системой безопасности. Переформулируйте его без попыток обхода инструкций.",
                        }
                    ]
                }

        return {"messages": []}

    def _route_after_guardrail(self, state: AgentState) -> str:
        last_message = state["messages"][-1]
        if last_message.get("role") == "assistant":
            return "end"
        return "agent"

    def _agent_node(self, state: AgentState) -> dict[str, Any]:
        allowed_dynamic_tools = set(state.get("allowed_dynamic_tools") or [])
        dynamic_schemas = (
            self.registry.get_ollama_tool_schemas(allowed_names=allowed_dynamic_tools)
            if self.settings.enable_dynamic_tools
            else []
        )
        response = None
        attempts = [
            {
                "tools": [web_search, web_fetch, DOCUMENT_RAG_TOOL_SCHEMA, *dynamic_schemas],
                "think": self.settings.ollama_think,
                "label": "primary",
            },
            {
                "tools": [web_search, web_fetch, DOCUMENT_RAG_TOOL_SCHEMA, *dynamic_schemas],
                "think": False,
                "label": "retry_no_think",
            },
            {
                "tools": [],
                "think": False,
                "label": "retry_minimal",
            },
        ]

        last_error: Exception | None = None
        for attempt in attempts:
            try:
                response = self.client.chat(
                    model=self.settings.ollama_model,
                    messages=state["messages"],
                    tools=attempt["tools"],
                    think=bool(attempt["think"]),
                )
                break
            except Exception as exc:
                last_error = exc
                logger.warning("Ollama chat attempt failed (%s): %s", attempt["label"], exc)

        if response is None:
            details = str(last_error) if last_error is not None else "unknown error"
            message = (
                "Model provider is temporarily unavailable (HTTP 500). "
                "Please retry in 10-30 seconds. "
                f"Details: {details}"
            )
            return {
                "messages": [
                    {
                        "role": "assistant",
                        "content": message,
                    }
                ]
            }

        assistant_message = self._normalize_assistant_message(response.message)
        usage = self._extract_token_usage(response)
        accumulated = TokenUsage.from_dict(state.get("token_usage"))
        accumulated.prompt_tokens += usage.prompt_tokens
        accumulated.completion_tokens += usage.completion_tokens
        accumulated.total_tokens += usage.total_tokens
        accumulated.request_count += 1

        return {
            "messages": [assistant_message],
            "token_usage": accumulated.to_dict(),
        }

    def _tools_node(self, state: AgentState) -> dict[str, Any]:
        tool_messages: list[dict[str, Any]] = []
        allowed_dynamic_tools = set(state.get("allowed_dynamic_tools") or [])
        dynamic_callables = (
            self.registry.get_callable_map(allowed_names=allowed_dynamic_tools)
            if self.settings.enable_dynamic_tools
            else {}
        )
        callables = {
            "web_search": web_search,
            "web_fetch": web_fetch,
            "document_rag": document_rag,
            **dynamic_callables,
        }

        last_message = state["messages"][-1]
        tool_calls = last_message.get("tool_calls") or []
        reminder_create_requested = self._has_reminder_create_call(tool_calls)

        for tool_call in tool_calls:
            function = tool_call.get("function") or {}
            tool_name = function.get("name", "unknown_tool")
            arguments = self._resolve_tool_arguments(
                tool_name=tool_name,
                raw_arguments=function.get("arguments"),
                state=state,
            )
            if reminder_create_requested and not self._is_reminder_create_execution(tool_name, arguments):
                content = (
                    "Skipped deferred-policy execution: reminder creation requests must only schedule work. "
                    "Generate no immediate report/file; the reminder worker will invoke LLM at trigger time."
                )
            else:
                content = self._execute_tool_call(
                    tool_name=tool_name,
                    arguments=arguments,
                    callables=callables,
                )

            file_payload = self._extract_file_payload_for_transport(content)
            content = self._sanitize_tool_content_for_model(content)
            model_limit = min(self.settings.max_tool_result_chars, _MAX_TOOL_CONTENT_CHARS_FOR_MODEL)
            content = content[:model_limit]
            tool_message = {
                "role": "tool",
                "tool_name": tool_name,
                "content": content,
            }
            if file_payload is not None:
                tool_message["file_payload"] = file_payload
            tool_messages.append(tool_message)

        return {
            "messages": tool_messages,
            "step_count": state["step_count"] + 1,
        }

    @staticmethod
    def _has_reminder_create_call(tool_calls: list[dict[str, Any]]) -> bool:
        for tool_call in tool_calls:
            function = tool_call.get("function") or {}
            tool_name = str(function.get("name") or "")
            if tool_name != "reminder_scheduler":
                continue
            arguments = OllamaLangGraphAgent._normalize_arguments(function.get("arguments"))
            if OllamaLangGraphAgent._extract_reminder_action(arguments) == "create":
                return True
        return False

    @staticmethod
    def _extract_reminder_action(arguments: dict[str, Any]) -> str:
        action = arguments.get("action")
        if isinstance(action, str) and action.strip():
            return action.strip().lower()

        nested = arguments.get("arguments")
        if isinstance(nested, dict):
            nested_action = nested.get("action")
            if isinstance(nested_action, str) and nested_action.strip():
                return nested_action.strip().lower()

        return ""

    @staticmethod
    def _is_reminder_create_execution(tool_name: str, arguments: dict[str, Any]) -> bool:
        if tool_name != "reminder_scheduler":
            return False
        return OllamaLangGraphAgent._extract_reminder_action(arguments) == "create"

    @staticmethod
    def _resolve_tool_arguments(
        *,
        tool_name: str,
        raw_arguments: Any,
        state: AgentState,
    ) -> dict[str, Any]:
        arguments = raw_arguments if isinstance(raw_arguments, dict) else {}

        if tool_name == "reminder_scheduler" and "chat_id" not in arguments:
            state_chat_id = state.get("chat_id")
            if isinstance(state_chat_id, int):
                arguments["chat_id"] = state_chat_id

        if tool_name == "reminder_scheduler":
            arguments.setdefault("org_id", str(state.get("org_id") or "default-org"))
            arguments.setdefault("team_id", str(state.get("team_id") or "chat"))
            state_user_id = state.get("user_id")
            if isinstance(state_user_id, int):
                arguments.setdefault("user_id", state_user_id)

        if tool_name == "document_rag":
            arguments.setdefault("org_id", str(state.get("org_id") or "default-org"))
            arguments.setdefault("team_id", str(state.get("team_id") or "chat"))
            state_user_id = state.get("user_id")
            if isinstance(state_user_id, int):
                arguments.setdefault("user_id", state_user_id)

        return arguments

    @staticmethod
    def _execute_tool_call(
        *,
        tool_name: str,
        arguments: dict[str, Any],
        callables: dict[str, Any],
    ) -> str:
        tool_fn = callables.get(tool_name)
        if tool_fn is None:
            return f"Tool {tool_name} not found"

        try:
            result = tool_fn(**arguments)
            return str(result)
        except Exception as exc:
            return f"Tool {tool_name} failed: {exc}"

    @staticmethod
    def _sanitize_tool_content_for_model(content: str) -> str:
        text = str(content or "")
        if not text:
            return ""

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text

        if not isinstance(parsed, dict):
            return text

        base64_data = parsed.get("base64")
        if isinstance(base64_data, str) and base64_data:
            parsed["base64"] = "<omitted>"
            parsed["base64_omitted"] = True
            parsed["base64_size_chars"] = len(base64_data)

        return json.dumps(parsed, ensure_ascii=True)

    @staticmethod
    def _extract_file_payload_for_transport(content: str) -> dict[str, Any] | None:
        text = str(content or "")
        if not text:
            return None

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None

        if not isinstance(parsed, dict):
            return None

        payload_type = str(parsed.get("type") or "").strip().lower()
        if payload_type not in {"file", "image"}:
            return None

        has_base64 = isinstance(parsed.get("base64"), str) and bool(parsed.get("base64"))
        has_path = isinstance(parsed.get("path"), str) and bool(str(parsed.get("path")).strip())
        if not has_base64 and not has_path:
            return None

        return parsed

    def _route_after_agent(self, state: AgentState) -> str:
        if state["step_count"] >= self.settings.agent_max_steps:
            return "end"

        last_message = state["messages"][-1]
        if last_message.get("tool_calls"):
            return "tools"
        return "end"

    @staticmethod
    def _last_assistant_message(messages: list[dict[str, Any]]) -> dict[str, Any]:
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                return msg
        return {"role": "assistant", "content": ""}

    @staticmethod
    def _normalize_assistant_message(raw_message: Any) -> dict[str, Any]:
        role = getattr(raw_message, "role", "assistant")
        content = getattr(raw_message, "content", "")
        thinking = getattr(raw_message, "thinking", None)
        raw_tool_calls = getattr(raw_message, "tool_calls", None) or []

        tool_calls = [OllamaLangGraphAgent._normalize_tool_call(tc) for tc in raw_tool_calls]

        message: dict[str, Any] = {
            "role": role,
            "content": content,
        }
        if thinking:
            message["thinking"] = thinking
        if tool_calls:
            message["tool_calls"] = tool_calls
        return message

    @staticmethod
    def _normalize_tool_call(raw_tool_call: Any) -> dict[str, Any]:
        if isinstance(raw_tool_call, dict):
            function = raw_tool_call.get("function") or {}
            name = function.get("name") or "unknown_tool"
            arguments = function.get("arguments") or {}
            return {
                "function": {
                    "name": name,
                    "arguments": OllamaLangGraphAgent._normalize_arguments(arguments),
                }
            }

        function = getattr(raw_tool_call, "function", None)
        name = getattr(function, "name", "unknown_tool")
        arguments = getattr(function, "arguments", {})
        return {
            "function": {
                "name": name,
                "arguments": OllamaLangGraphAgent._normalize_arguments(arguments),
            }
        }

    @staticmethod
    def _normalize_arguments(arguments: Any) -> dict[str, Any]:
        if isinstance(arguments, dict):
            return arguments
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
            except json.JSONDecodeError:
                return {"input": arguments}
            return parsed if isinstance(parsed, dict) else {"input": arguments}
        return {}

    @staticmethod
    def _extract_token_usage(response: Any) -> TokenUsage:
        if isinstance(response, dict):
            prompt = int(response.get("prompt_eval_count") or 0)
            completion = int(response.get("eval_count") or 0)
            total = int(response.get("total_tokens") or (prompt + completion))
            return TokenUsage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)

        prompt = int(getattr(response, "prompt_eval_count", 0) or 0)
        completion = int(getattr(response, "eval_count", 0) or 0)
        total = int(getattr(response, "total_tokens", 0) or (prompt + completion))
        return TokenUsage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)
