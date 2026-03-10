"""Pydantic v2 schemas for LangGraph agent state and structured LLM outputs.

These schemas enforce strict typing on every LLM response, preventing
the system from crashing when a model returns unexpected data.
"""
from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Router decision — the LLM must return exactly this schema
# ---------------------------------------------------------------------------

class RouterDecision(str, Enum):
    """Possible decisions the router node can make."""
    TOOL = "tool"
    CHAT = "chat"
    MEMORY = "memory"
    CLARIFY = "clarify"
    WEB_SEARCH = "web_search"


class ToolStep(BaseModel):
    """A single planned tool invocation."""
    tool: str = Field(description="Tool registry name, e.g. 'cron_add'")
    arguments: dict[str, Any] = Field(default_factory=dict, description="Validated arguments for the tool")


class IntegrationCallArgs(BaseModel):
    """Structured arguments for integration_call extracted by the router LLM."""
    service_name: str = Field(description="Service name of the target integration")
    url: str | None = Field(default=None, description="Specific endpoint URL (optional, uses default if omitted)")
    method: str = Field(default="GET", description="HTTP method")
    params: dict[str, Any] = Field(default_factory=dict, description="Query / URL template params")
    payload: dict[str, Any] | None = Field(default=None, description="JSON body for POST/PUT")


class IntegrationInfo(BaseModel):
    """Compact integration descriptor injected into the router context."""
    service_name: str
    endpoints: list[str] = Field(default_factory=list, description="Available endpoint URLs")


class RouterOutput(BaseModel):
    """Structured output the router LLM must return."""
    decision: RouterDecision = Field(description="'tool' — invoke tools, 'chat' — direct reply, 'memory' — memory op, 'clarify' — ask user")
    steps: list[ToolStep] = Field(default_factory=list, description="Ordered tool steps (1-3), empty if not 'tool'")
    response_hint: str = Field(default="", description="Optional hint for the final answer composer")
    confidence: float = Field(default=0.8, ge=0.0, le=1.0, description="Router confidence in the decision")


# ---------------------------------------------------------------------------
# Tool execution result
# ---------------------------------------------------------------------------

class ToolResult(BaseModel):
    """Result from executing a single tool step."""
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    success: bool = False
    result: dict[str, Any] | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Memory extraction — semantic entity extraction from user messages
# ---------------------------------------------------------------------------

class ExtractedEntity(BaseModel):
    """An entity/fact extracted from user speech for semantic memory."""
    entity_type: str = Field(description="e.g. 'location', 'preference', 'name', 'timezone'")
    key: str = Field(description="Normalized key, e.g. 'city'")
    value: str = Field(description="Extracted value, e.g. 'Москва'")
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)


class MemoryExtractionOutput(BaseModel):
    """Structured output for semantic memory extraction."""
    entities: list[ExtractedEntity] = Field(default_factory=list)
    should_store: bool = Field(default=False, description="Whether any entities merit long-term storage")


# ---------------------------------------------------------------------------
# Guardrail check result
# ---------------------------------------------------------------------------

class GuardrailVerdict(str, Enum):
    PASS = "pass"
    WARN = "warn"
    BLOCK = "block"


class GuardrailResult(BaseModel):
    """Result of a guardrail/safety check on input or output."""
    verdict: GuardrailVerdict = GuardrailVerdict.PASS
    reason: str = ""
    modified_text: str | None = None


# ---------------------------------------------------------------------------
# Agent State — the central state object flowing through the LangGraph
# ---------------------------------------------------------------------------

class AgentState(BaseModel):
    """State that flows through every node of the LangGraph agent.

    This is the single source of truth during a request lifecycle.
    All nodes read from and write to this state.
    """
    # --- Request context ---
    user_id: UUID
    session_id: UUID
    user_message: str
    system_prompt: str = ""
    permissions: list[str] = Field(default_factory=list)

    # --- Router ---
    router_output: RouterOutput | None = None

    # --- Memory context ---
    history_messages: list[dict[str, str]] = Field(default_factory=list)
    stm_context: list[str] = Field(default_factory=list)
    ltm_context: list[str] = Field(default_factory=list)
    rag_context: list[str] = Field(default_factory=list)
    history_summary: str | None = None
    extracted_entities: list[ExtractedEntity] = Field(default_factory=list)

    # --- Tool execution ---
    tool_results: list[ToolResult] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)

    # --- Guardrails ---
    input_guardrail: GuardrailResult = Field(default_factory=GuardrailResult)
    output_guardrail: GuardrailResult = Field(default_factory=GuardrailResult)

    # --- Final output ---
    final_answer: str = ""
    tool_calls_log: list[dict[str, Any]] = Field(default_factory=list)

    # --- Control flow ---
    next_step: str = ""
    iteration: int = 0
    max_iterations: int = 3
    error: str | None = None

    model_config = {"arbitrary_types_allowed": True}
