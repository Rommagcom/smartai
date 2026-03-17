"""Typed graph contexts extracted from raw LangGraph state.

These DTOs define explicit read contracts for node/pipeline inputs and remove
scattered direct `state.get(...)` usage from orchestration code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.schemas.graph import ToolResult


@dataclass
class RouterContext:
    """Typed context for router node input."""

    user_message: str
    user_id: Any = None
    feedback_plan: str = ""
    retrieved_tools: list[dict] = field(default_factory=list)
    history_messages: list[dict] = field(default_factory=list)

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> RouterContext:
        """Build RouterContext from raw graph state."""

        return cls(
            user_message=str(state.get("user_message") or ""),
            user_id=state.get("user_id"),
            feedback_plan=str(state.get("feedback_plan") or "").strip(),
            retrieved_tools=state.get("retrieved_tools") or [],
            history_messages=state.get("history_messages") or [],
        )


@dataclass
class ComposeContext:
    """Typed context for compose pipeline input extraction."""

    messages: list[str] = field(default_factory=list)
    user_message: str = ""
    history_messages: list[dict] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    web_fetch_content: str = ""
    web_search_results: list[dict] = field(default_factory=list)
    final_answer: str = ""
    state_context: list[str] = field(default_factory=list)
    iteration: int | None = None
    iterations: int | None = None
    max_iterations: int | None = None

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> ComposeContext:
        """Build ComposeContext from raw graph state."""

        return cls(
            messages=state.get("messages") or [],
            user_message=str(state.get("user_message") or ""),
            history_messages=state.get("history_messages") or [],
            tool_results=state.get("tool_results") or [],
            web_fetch_content=str(state.get("web_fetch_content") or ""),
            web_search_results=state.get("web_search_results") or [],
            final_answer=str(state.get("final_answer") or ""),
            state_context=state.get("context") or [],
            iteration=state.get("iteration"),
            iterations=state.get("iterations"),
            max_iterations=state.get("max_iterations"),
        )

    @property
    def current_user_message(self) -> str:
        """Get current user message with messages[-1] priority."""

        if self.messages:
            return str(self.messages[-1] or "")
        return self.user_message


@dataclass
class OutputContext:
    """Typed context for output pipeline input extraction."""

    final_answer: str = ""
    user_message: str = ""
    user_id: Any = None
    web_fetch_content: str = ""
    web_search_results: list[dict] = field(default_factory=list)
    tool_results: list[Any] = field(default_factory=list)
    tool_calls_log: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> OutputContext:
        """Build OutputContext from raw graph state."""

        return cls(
            final_answer=str(state.get("final_answer") or ""),
            user_message=str(state.get("user_message") or ""),
            user_id=state.get("user_id"),
            web_fetch_content=str(state.get("web_fetch_content") or ""),
            web_search_results=state.get("web_search_results") or [],
            tool_results=state.get("tool_results") or [],
            tool_calls_log=state.get("tool_calls_log") or [],
            artifacts=state.get("artifacts") or [],
        )
