"""Shared orchestration types for graph control flow.

Removes stringly-typed routing by centralizing next-step and fallback modes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.schemas.graph import GuardrailResult, RouterDecision, RouterOutput, ToolResult


class NextStep(str, Enum):
    """Canonical graph next-step values stored in state."""

    END = "end"
    CHAT = "chat"
    RETRIEVER = "retriever"
    TOOL = "tool"
    WEB_SEARCH = "web_search"
    WEB_FETCH = "web_fetch"
    COMPOSE = "compose"
    OUTPUT = "output"


class IntentLabel(str, Enum):
    """Canonical micro-router intent values."""

    SMALL_TALK = "small_talk"
    NEEDS_TOOLS = "needs_tools"


class ComposeLoopDecision(str, Enum):
    """Compose loop branch labels for conditional edges."""

    FINISH = "finish"
    CONTINUE = "continue"


class FallbackMode(str, Enum):
    """Fallback mode labels for router recovery and observability."""

    JSON_SALVAGE = "json_salvage"
    EXPLICIT_EXPORT = "explicit_export"
    LIVE_EXPORT = "live_export"
    WEB_SEARCH = "web_search"
    CHAT = "chat"


class SystemToolName(str, Enum):
    """Canonical system tool names used in orchestration.

    Centralizes all magic tool name strings ("web_search", "cron_add", etc.)
    into a typed enum for router, planner, and orchestrator layers.
    """

    # Search and document tools
    WEB_SEARCH = "web_search"
    DOC_ASK = "doc_ask"
    DOC_SEARCH = "doc_search"
    DOC_LIST = "doc_list"
    DOC_DELETE = "doc_delete"
    DOC_DELETE_ALL = "doc_delete_all"

    # Memory tools
    MEMORY_ADD = "memory_add"
    MEMORY_LIST = "memory_list"
    MEMORY_SEARCH = "memory_search"
    MEMORY_DELETE = "memory_delete"
    MEMORY_DELETE_ALL = "memory_delete_all"

    # Scheduling and reminders
    CRON_ADD = "cron_add"
    CRON_LIST = "cron_list"
    CRON_DELETE = "cron_delete"
    CRON_DELETE_ALL = "cron_delete_all"

    # Export and formatting
    PDF_CREATE = "pdf_create"
    EXCEL_CREATE = "excel_create"

    # Integration management
    INTEGRATION_ONBOARDING_CONNECT = "integration_onboarding_connect"
    INTEGRATION_ONBOARDING_TEST = "integration_onboarding_test"
    INTEGRATION_ONBOARDING_SAVE = "integration_onboarding_save"
    INTEGRATION_HEALTH = "integration_health"
    INTEGRATIONS_DELETE_ALL = "integrations_delete_all"

    # Dynamic tools
    DYNAMIC_TOOL_REGISTER = "dynamic_tool_register"
    DYNAMIC_TOOL_LIST = "dynamic_tool_list"
    DYNAMIC_TOOL_DELETE = "dynamic_tool_delete"
    DYNAMIC_TOOL_DELETE_ALL = "dynamic_tool_delete_all"
    REGISTER_API_TOOL = "register_api_tool"

    # Worker enqueuing
    WORKER_ENQUEUE = "worker_enqueue"


@dataclass
class NodeUpdate:
    """Typed state update returned by graph nodes.

    Keeps `next_step` typed while allowing incremental adoption for other
    state fields via flexible `values` payload.
    """

    values: dict[str, Any] = field(default_factory=dict)
    next_step: NextStep | None = None

    def to_state_update(self) -> dict[str, Any]:
        update = dict(self.values)
        if self.next_step is not None:
            update["next_step"] = self.next_step.value
        return update


@dataclass
class RouterNodeUpdate:
    """Typed update for router node outputs."""

    router_output: RouterOutput
    next_step: NextStep
    values: dict[str, Any] = field(default_factory=dict)

    def to_state_update(self) -> dict[str, Any]:
        update = {
            "router_output": self.router_output,
            "next_step": self.next_step.value,
        }
        update.update(self.values)
        return update


@dataclass
class ComposeNodeUpdate:
    """Typed update for compose node outputs."""

    final_answer: str
    is_complete: bool
    feedback_plan: str
    iterations: int
    iteration: int

    def to_state_update(self) -> dict[str, Any]:
        return {
            "final_answer": self.final_answer,
            "is_complete": self.is_complete,
            "feedback_plan": self.feedback_plan,
            "iterations": self.iterations,
            "iteration": self.iteration,
        }


@dataclass
class OutputNodeUpdate:
    """Typed update for output node outputs."""

    final_answer: str
    output_guardrail: tuple[bool, str]
    tool_calls_log: list[dict]
    artifacts: list[dict]

    def to_state_update(self) -> dict[str, Any]:
        return {
            "final_answer": self.final_answer,
            "output_guardrail": self.output_guardrail,
            "tool_calls_log": self.tool_calls_log,
            "artifacts": self.artifacts,
        }


@dataclass
class GuardrailNodeUpdate:
    """Typed update for input guardrail outputs."""

    input_guardrail: GuardrailResult
    final_answer: str | None = None
    next_step: NextStep | None = None

    def to_state_update(self) -> dict[str, Any]:
        update: dict[str, Any] = {
            "input_guardrail": self.input_guardrail,
        }
        if self.final_answer is not None:
            update["final_answer"] = self.final_answer
        if self.next_step is not None:
            update["next_step"] = self.next_step.value
        return update


@dataclass
class ToolExecutionNodeUpdate:
    """Typed update for tool execution node outputs."""

    tool_results: list[ToolResult]
    next_step: NextStep
    artifacts: list[dict] = field(default_factory=list)
    tool_calls_log: list[dict] = field(default_factory=list)
    error: str | None = None

    def to_state_update(self) -> dict[str, Any]:
        update: dict[str, Any] = {
            "tool_results": self.tool_results,
            "next_step": self.next_step.value,
        }
        if self.artifacts:
            update["artifacts"] = self.artifacts
        if self.tool_calls_log:
            update["tool_calls_log"] = self.tool_calls_log
        if self.error:
            update["error"] = self.error
        return update


def coerce_next_step(value: object, default: NextStep = NextStep.CHAT) -> NextStep:
    """Convert arbitrary next_step value to NextStep enum with safe default."""

    raw = str(value or "").strip().lower()
    for step in NextStep:
        if step.value == raw:
            return step
    return default


def coerce_intent_label(value: object, default: IntentLabel = IntentLabel.NEEDS_TOOLS) -> IntentLabel:
    """Convert arbitrary intent value to IntentLabel enum with safe default."""

    raw = str(value or "").strip().lower()
    for intent in IntentLabel:
        if intent.value == raw:
            return intent
    return default


def coerce_tool_name(value: object, default: SystemToolName | None = None) -> SystemToolName | None:
    """Convert arbitrary tool name value to SystemToolName enum with safe default.

    Returns None if unknown tool (allows for user-defined/dynamic tools).
    """

    raw = str(value or "").strip().lower()
    if not raw:
        return default

    for tool in SystemToolName:
        if tool.value == raw:
            return tool
    return default


def next_step_from_router_decision(decision: RouterDecision) -> NextStep:
    """Map RouterDecision to concrete runtime next step.

    Router supports abstract decisions (`memory`, `clarify`) that both route
    to `chat` in the current graph topology.
    """

    if decision == RouterDecision.TOOL:
        return NextStep.TOOL
    if decision == RouterDecision.WEB_SEARCH:
        return NextStep.WEB_SEARCH
    return NextStep.CHAT
