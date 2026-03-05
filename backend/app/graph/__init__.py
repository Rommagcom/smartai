"""LangGraph agent builder — assembles the state graph.

Graph topology:
                         ┌──────────────┐
                         │   START      │
                         └──────┬───────┘
                                │
                         ┌──────▼───────┐
                         │  guardrail   │──── (block) ───→ END
                         └──────┬───────┘
                                │ (pass)
                         ┌──────▼───────┐
                         │   memory     │
                         └──────┬───────┘
                                │
                         ┌──────▼───────┐
                    ┌────│   router     │────┐
                    │    └──────┬───────┘    │
                    │           │            │
              ┌─────▼──┐ ┌─────▼──┐  ┌──────▼─────┐
              │  tool   │ │  chat  │  │  clarify   │
              └────┬────┘ └────┬───┘  └─────┬──────┘
                   │           │            │
              ┌────▼────┐     │            │
              │ compose  │     │            │
              └────┬────┘     │            │
                   │           │            │
                   └─────┬─────┘────────────┘
                         │
                  ┌──────▼───────┐
                  │   output     │
                  └──────┬───────┘
                         │
                  ┌──────▼───────┐
                  │     END      │
                  └──────────────┘
"""
from __future__ import annotations

import logging
import operator
from typing import Annotated, Any, TypedDict
from uuid import UUID

from langgraph.graph import END, StateGraph

from app.graph.nodes import (
    chat_node,
    compose_node,
    input_guardrail_node,
    memory_node,
    output_node,
    router_node,
    tool_execution_node,
)
from app.schemas.graph import (
    ExtractedEntity,
    GuardrailResult,
    GuardrailVerdict,
    RouterOutput,
    ToolResult,
)

logger = logging.getLogger(__name__)


def _replace_value(a: Any, b: Any) -> Any:
    """Reducer: always take the newer value."""
    return b if b is not None else a


# LangGraph requires a TypedDict with reducers for concurrent updates
class GraphState(TypedDict, total=False):
    user_id: UUID
    session_id: UUID
    user_message: str
    system_prompt: str
    permissions: list[str]

    # Router
    router_output: Annotated[RouterOutput | None, _replace_value]

    # Memory
    history_messages: Annotated[list[dict], _replace_value]
    stm_context: Annotated[list[str], _replace_value]
    ltm_context: Annotated[list[str], _replace_value]
    rag_context: Annotated[list[str], _replace_value]
    history_summary: Annotated[str | None, _replace_value]
    extracted_entities: Annotated[list[ExtractedEntity], _replace_value]

    # Tool execution
    tool_results: Annotated[list[ToolResult], _replace_value]
    artifacts: Annotated[list[dict], _replace_value]

    # Guardrails
    input_guardrail: Annotated[GuardrailResult | None, _replace_value]
    output_guardrail: Annotated[GuardrailResult | None, _replace_value]

    # Output
    final_answer: Annotated[str, _replace_value]
    tool_calls_log: Annotated[list[dict], _replace_value]

    # Control
    next_step: Annotated[str, _replace_value]
    iteration: int
    max_iterations: int
    error: Annotated[str | None, _replace_value]


def _route_after_guardrail(state: dict) -> str:
    """Route after input guardrail: blocked → end, otherwise → memory."""
    guardrail = state.get("input_guardrail")
    if guardrail and guardrail.verdict == GuardrailVerdict.BLOCK:
        return "output"
    return "memory"


def _route_after_router(state: dict) -> str:
    """Route based on the router decision."""
    next_step = state.get("next_step", "chat")
    router_output = state.get("router_output")

    if next_step == "end":
        return "output"

    if router_output:
        decision = router_output.decision.value
        if decision == "tool":
            return "tool_exec"
        if decision == "memory":
            return "chat"
        if decision == "clarify":
            return "chat"
    return "chat"


def _route_after_tool_exec(state: dict) -> str:
    """After tool execution, always compose the final answer."""
    return "compose"


def build_agent_graph() -> StateGraph:
    """Build and compile the LangGraph agent.

    Returns a compiled graph that can be invoked with:
        result = await graph.ainvoke(initial_state)
    """
    workflow = StateGraph(GraphState)

    # Add nodes
    workflow.add_node("guardrail", input_guardrail_node)
    workflow.add_node("memory", memory_node)
    workflow.add_node("router", router_node)
    workflow.add_node("tool_exec", tool_execution_node)
    workflow.add_node("chat", chat_node)
    workflow.add_node("compose", compose_node)
    workflow.add_node("output", output_node)

    # Set entry point
    workflow.set_entry_point("guardrail")

    # Conditional edges
    workflow.add_conditional_edges("guardrail", _route_after_guardrail, {
        "memory": "memory",
        "output": "output",
    })

    workflow.add_edge("memory", "router")

    workflow.add_conditional_edges("router", _route_after_router, {
        "tool_exec": "tool_exec",
        "chat": "chat",
        "output": "output",
    })

    workflow.add_conditional_edges("tool_exec", _route_after_tool_exec, {
        "compose": "compose",
    })

    workflow.add_edge("chat", "output")
    workflow.add_edge("compose", "output")
    workflow.add_edge("output", END)

    return workflow.compile()


# Singleton compiled graph
agent_graph = build_agent_graph()
