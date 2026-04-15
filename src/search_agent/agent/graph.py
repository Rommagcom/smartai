from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from langsmith import traceable

from search_agent.agent.ollama_tools import web_fetch, web_search
from search_agent.agent.prompts import SYSTEM_PROMPT
from search_agent.config import Settings
from search_agent.dynamic_skills.registry import DynamicToolRegistry


_MAX_INPUT_CHARS = 8000
_PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior)\s+instructions", re.IGNORECASE),
    re.compile(r"(system|developer)\s+prompt", re.IGNORECASE),
    re.compile(r"reveal\s+(your\s+)?(instructions|prompt)", re.IGNORECASE),
    re.compile(r"jailbreak", re.IGNORECASE),
]


@dataclass(slots=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    request_count: int = 0


@dataclass(slots=True)
class AgentRunResult:
    answer: str
    thinking: str | None
    messages: list[dict[str, Any]]
    token_usage: TokenUsage


@dataclass(slots=True)
class _RunContext:
    chat_id: int | None
    org_id: str
    user_id: int
    allowed_dynamic_tools: set[str]
    reminder_create_requested: bool = False


class OllamaLangGraphAgent:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.registry = DynamicToolRegistry(settings.dynamic_skills_dir)

    def refresh_dynamic_tools(self) -> None:
        self.registry.refresh()

    @traceable(name="deepagents_agent_run")
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
        del team_id, role

        if self.settings.enable_dynamic_tools:
            self.refresh_dynamic_tools()
        self._configure_langsmith_env()

        guardrail_text = self._guardrail_message(user_message)
        if guardrail_text:
            return AgentRunResult(
                answer=guardrail_text,
                thinking=None,
                messages=[{"role": "assistant", "content": guardrail_text}],
                token_usage=TokenUsage(),
            )

        run_ctx = _RunContext(
            chat_id=chat_id,
            org_id=org_id,
            user_id=int(user_id),
            allowed_dynamic_tools=set(allowed_dynamic_tools or set()),
        )

        # Import lazily so module import and unit test collection do not require
        # full runtime LLM dependencies unless run() is actually executed.
        from deepagents import create_deep_agent
        from langchain_ollama import ChatOllama

        model = ChatOllama(
            model=self.settings.ollama_model,
            base_url=self.settings.ollama_base_url or "http://localhost:11434",
            temperature=0,
        )
        agent = create_deep_agent(
            model=model,
            tools=self._build_tools(run_ctx),
            system_prompt=SYSTEM_PROMPT,
        )

        input_messages = self._build_input_messages(history_messages or [], user_message)
        try:
            raw_result = agent.invoke({"messages": input_messages})
        except Exception as exc:
            message = (
                "Model provider is temporarily unavailable (HTTP 500). "
                "Please retry in 10-30 seconds. "
                f"Details: {exc}"
            )
            return AgentRunResult(
                answer=message,
                thinking=None,
                messages=[{"role": "assistant", "content": message}],
                token_usage=TokenUsage(),
            )

        raw_messages = self._extract_raw_messages(raw_result)
        normalized_messages = self._normalize_messages(raw_messages)
        last_assistant = self._last_assistant_message(normalized_messages)
        usage = self._extract_token_usage(raw_messages)

        return AgentRunResult(
            answer=str(last_assistant.get("content") or ""),
            thinking=last_assistant.get("thinking"),
            messages=normalized_messages,
            token_usage=usage,
        )

    def _configure_langsmith_env(self) -> None:
        if not self.settings.langsmith_tracing:
            return
        os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
        os.environ.setdefault("LANGSMITH_PROJECT", self.settings.langsmith_project)

    @staticmethod
    def _guardrail_message(user_message: str) -> str:
        text = str(user_message or "")
        if len(text) > _MAX_INPUT_CHARS:
            return "Запрос слишком длинный. Пожалуйста, сократите текст и попробуйте снова."

        for pattern in _PROMPT_INJECTION_PATTERNS:
            if pattern.search(text):
                return "Запрос отклонен системой безопасности. Переформулируйте его без попыток обхода инструкций."
        return ""

    def _build_tools(self, run_ctx: _RunContext) -> list[Any]:
        from langchain_core.tools import tool

        @tool("web_search")
        def web_search_tool(query: str) -> str:
            """Search the web for the given query."""
            return str(web_search(query))

        @tool("web_fetch")
        def web_fetch_tool(url: str) -> str:
            """Fetch webpage content for a URL."""
            return str(web_fetch(url))

        @tool("list_dynamic_skills")
        def list_dynamic_skills_tool() -> str:
            """List dynamic skills currently available for this user session."""
            if not self.settings.enable_dynamic_tools:
                return json.dumps({"count": 0, "skills": []}, ensure_ascii=True)

            visible = self._visible_dynamic_tools(run_ctx)
            payload = {
                "count": len(visible),
                "skills": [
                    {
                        "name": name,
                        "description": tool_item.description,
                    }
                    for name, tool_item in visible
                ],
            }
            return json.dumps(payload, ensure_ascii=True)

        @tool("run_dynamic_skill")
        def run_dynamic_skill_tool(tool_name: str, arguments_json: str = "{}") -> str:
            """Run one dynamic skill by name with JSON object arguments."""
            normalized_name = str(tool_name or "").strip()
            if not normalized_name:
                raise ValueError("tool_name is required")

            callables = dict(self._visible_dynamic_tools(run_ctx, callable_map=True))
            tool_fn = callables.get(normalized_name)
            if tool_fn is None:
                allowed = sorted(callables.keys())
                raise ValueError(f"Unknown or forbidden tool: {normalized_name}. Allowed: {', '.join(allowed)}")

            args = self._parse_arguments_json(arguments_json)
            args = self._inject_reminder_context(tool_name=normalized_name, arguments=args, run_ctx=run_ctx)

            # Preserve deferred reminder policy: after reminder create, skip non-create tool execution.
            if run_ctx.reminder_create_requested and not self._is_reminder_create_execution(normalized_name, args):
                return (
                    "Skipped deferred-policy execution: reminder creation requests must only schedule work. "
                    "Generate no immediate report/file; the reminder worker will invoke LLM at trigger time."
                )

            result = tool_fn(**args)
            if self._is_reminder_create_execution(normalized_name, args):
                run_ctx.reminder_create_requested = True
            return str(result)

        return [web_search_tool, web_fetch_tool, list_dynamic_skills_tool, run_dynamic_skill_tool]

    def _visible_dynamic_tools(self, run_ctx: _RunContext, callable_map: bool = False) -> list[tuple[str, Any]]:
        if not self.settings.enable_dynamic_tools:
            return []

        if not self.registry.tools:
            self.refresh_dynamic_tools()

        if run_ctx.allowed_dynamic_tools:
            names = sorted(name for name in self.registry.tools if name in run_ctx.allowed_dynamic_tools)
        else:
            names = sorted(self.registry.tools.keys())

        if callable_map:
            callables = self.registry.get_callable_map(allowed_names=set(names))
            return [(name, callables[name]) for name in names if name in callables]

        return [(name, self.registry.tools[name]) for name in names]

    @staticmethod
    def _parse_arguments_json(arguments_json: str) -> dict[str, Any]:
        raw = str(arguments_json or "{}").strip() or "{}"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"arguments_json must be valid JSON object: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("arguments_json must decode to JSON object")
        return parsed

    @staticmethod
    def _inject_reminder_context(*, tool_name: str, arguments: dict[str, Any], run_ctx: _RunContext) -> dict[str, Any]:
        if tool_name != "reminder_scheduler":
            return arguments

        updated = dict(arguments)
        updated.setdefault("org_id", run_ctx.org_id or "default-org")
        updated.setdefault("user_id", int(run_ctx.user_id))
        if run_ctx.chat_id is not None:
            updated.setdefault("chat_id", int(run_ctx.chat_id))
        return updated

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

    def _tools_node(self, state: dict[str, Any]) -> dict[str, Any]:
        """Backward-compatible tool executor used by existing reminder policy tests."""
        tool_messages: list[dict[str, Any]] = []
        allowed_dynamic_tools = set(state.get("allowed_dynamic_tools") or [])
        dynamic_callables = (
            self.registry.get_callable_map(allowed_names=allowed_dynamic_tools)
            if getattr(self.settings, "enable_dynamic_tools", False)
            else {}
        )
        callables = {
            "web_search": web_search,
            "web_fetch": web_fetch,
            **dynamic_callables,
        }

        messages = state.get("messages") or []
        last_message = messages[-1] if messages else {}
        tool_calls = last_message.get("tool_calls") or []
        reminder_create_requested = any(
            self._is_reminder_create_execution(
                str((tool_call.get("function") or {}).get("name") or ""),
                self._normalize_arguments((tool_call.get("function") or {}).get("arguments")),
            )
            for tool_call in tool_calls
        )

        for tool_call in tool_calls:
            function = tool_call.get("function") or {}
            tool_name = str(function.get("name") or "unknown_tool")
            arguments = self._normalize_arguments(function.get("arguments"))

            if tool_name == "reminder_scheduler":
                chat_id = state.get("chat_id")
                org_id = str(state.get("org_id") or "default-org")
                user_id = state.get("user_id")
                if chat_id is not None:
                    arguments.setdefault("chat_id", int(chat_id))
                arguments.setdefault("org_id", org_id)
                if isinstance(user_id, int):
                    arguments.setdefault("user_id", user_id)

            if reminder_create_requested and not self._is_reminder_create_execution(tool_name, arguments):
                content = (
                    "Skipped deferred-policy execution: reminder creation requests must only schedule work. "
                    "Generate no immediate report/file; the reminder worker will invoke LLM at trigger time."
                )
            else:
                content = self._execute_tool_call(tool_name=tool_name, arguments=arguments, callables=callables)

            tool_messages.append(
                {
                    "role": "tool",
                    "tool_name": tool_name,
                    "content": str(content),
                }
            )

        return {
            "messages": tool_messages,
            "step_count": int(state.get("step_count") or 0) + 1,
        }

    @staticmethod
    def _execute_tool_call(*, tool_name: str, arguments: dict[str, Any], callables: dict[str, Any]) -> str:
        tool_fn = callables.get(tool_name)
        if tool_fn is None:
            return f"Tool {tool_name} not found"
        try:
            return str(tool_fn(**arguments))
        except Exception as exc:
            return f"Tool {tool_name} failed: {exc}"

    @staticmethod
    def _build_input_messages(history_messages: list[dict[str, Any]], user_message: str) -> list[dict[str, str]]:
        output: list[dict[str, str]] = []
        for item in history_messages:
            role = str(item.get("role") or "").strip().lower()
            if role not in {"system", "user", "assistant"}:
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            output.append({"role": role, "content": content})

        output.append({"role": "user", "content": str(user_message or "")})
        return output

    @staticmethod
    def _extract_raw_messages(raw_result: Any) -> list[Any]:
        if isinstance(raw_result, dict):
            messages = raw_result.get("messages")
            if isinstance(messages, list):
                return messages
        return []

    @staticmethod
    def _normalize_messages(messages: list[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in messages:
            msg = OllamaLangGraphAgent._normalize_message(item)
            if msg is not None:
                normalized.append(msg)
        return normalized

    @staticmethod
    def _normalize_message(raw: Any) -> dict[str, Any] | None:
        if isinstance(raw, dict):
            role = str(raw.get("role") or "").strip().lower()
            if role in {"system", "user", "assistant", "tool"}:
                msg = {"role": role, "content": OllamaLangGraphAgent._normalize_content(raw.get("content"))}
                tool_calls = raw.get("tool_calls")
                if isinstance(tool_calls, list) and tool_calls:
                    msg["tool_calls"] = OllamaLangGraphAgent._normalize_tool_calls(tool_calls)
                if role == "tool" and raw.get("tool_name"):
                    msg["tool_name"] = str(raw.get("tool_name"))
                return msg
            return None

        msg_type = str(getattr(raw, "type", "")).strip().lower()
        role_map = {
            "system": "system",
            "human": "user",
            "ai": "assistant",
            "tool": "tool",
        }
        role = role_map.get(msg_type)
        if not role:
            return None

        content = OllamaLangGraphAgent._normalize_content(getattr(raw, "content", ""))
        message: dict[str, Any] = {"role": role, "content": content}

        if role == "assistant":
            raw_tool_calls = getattr(raw, "tool_calls", None)
            if isinstance(raw_tool_calls, list) and raw_tool_calls:
                message["tool_calls"] = OllamaLangGraphAgent._normalize_tool_calls(raw_tool_calls)
        thinking = getattr(raw, "reasoning", None) or getattr(raw, "thinking", None)
        if role == "assistant" and isinstance(thinking, str) and thinking.strip():
            message["thinking"] = thinking.strip()
        if role == "tool":
            tool_name = str(getattr(raw, "name", "") or "").strip()
            if tool_name:
                message["tool_name"] = tool_name
        return message

    @staticmethod
    def _normalize_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    value = str(part.get("text") or "")
                    if value:
                        chunks.append(value)
            return "\n".join(chunks)
        return str(content or "")

    @staticmethod
    def _normalize_tool_calls(raw_tool_calls: list[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for call in raw_tool_calls:
            if isinstance(call, dict):
                name = str(call.get("name") or call.get("tool_name") or "").strip()
                args = call.get("args", call.get("arguments", {}))
            else:
                name = str(getattr(call, "name", "") or getattr(call, "tool_name", "")).strip()
                args = getattr(call, "args", getattr(call, "arguments", {}))

            if not name:
                continue
            parsed_args = OllamaLangGraphAgent._normalize_arguments(args)
            normalized.append({"function": {"name": name, "arguments": parsed_args}})
        return normalized

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
    def _last_assistant_message(messages: list[dict[str, Any]]) -> dict[str, Any]:
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                return msg
        return {"role": "assistant", "content": ""}

    @staticmethod
    def _extract_token_usage(raw_messages: list[Any]) -> TokenUsage:
        usage = TokenUsage()
        saw_usage = False
        for message in raw_messages:
            msg_type = OllamaLangGraphAgent._message_type(message)
            if msg_type != "ai":
                continue

            usage_metadata = OllamaLangGraphAgent._message_usage_metadata(message)
            if not usage_metadata:
                continue

            prompt = OllamaLangGraphAgent._coerce_int(
                usage_metadata.get("input_tokens")
                or usage_metadata.get("prompt_tokens")
                or usage_metadata.get("prompt_eval_count")
            )
            completion = OllamaLangGraphAgent._coerce_int(
                usage_metadata.get("output_tokens")
                or usage_metadata.get("completion_tokens")
                or usage_metadata.get("eval_count")
            )
            total = OllamaLangGraphAgent._coerce_int(
                usage_metadata.get("total_tokens")
                or usage_metadata.get("total_token_count")
                or (prompt + completion)
            )

            usage.prompt_tokens += prompt
            usage.completion_tokens += completion
            usage.total_tokens += total
            usage.request_count += 1
            saw_usage = True
        if not saw_usage:
            usage.request_count = 1
        return usage

    @staticmethod
    def _message_type(message: Any) -> str:
        if isinstance(message, dict):
            return str(message.get("type") or message.get("role") or "").strip().lower()
        return str(getattr(message, "type", "") or getattr(message, "role", "")).strip().lower()

    @staticmethod
    def _message_usage_metadata(message: Any) -> dict[str, Any] | None:
        if isinstance(message, dict):
            direct = message.get("usage_metadata")
            if isinstance(direct, dict):
                return direct
            response_metadata = message.get("response_metadata")
            if isinstance(response_metadata, dict):
                prompt = OllamaLangGraphAgent._coerce_int(response_metadata.get("prompt_eval_count"))
                completion = OllamaLangGraphAgent._coerce_int(response_metadata.get("eval_count"))
                if prompt or completion:
                    return {
                        "prompt_eval_count": prompt,
                        "eval_count": completion,
                        "total_tokens": prompt + completion,
                    }
            return None

        direct = getattr(message, "usage_metadata", None)
        if isinstance(direct, dict):
            return direct

        response_metadata = getattr(message, "response_metadata", None)
        if isinstance(response_metadata, dict):
            prompt = OllamaLangGraphAgent._coerce_int(response_metadata.get("prompt_eval_count"))
            completion = OllamaLangGraphAgent._coerce_int(response_metadata.get("eval_count"))
            if prompt or completion:
                return {
                    "prompt_eval_count": prompt,
                    "eval_count": completion,
                    "total_tokens": prompt + completion,
                }
        return None

    @staticmethod
    def _coerce_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0
