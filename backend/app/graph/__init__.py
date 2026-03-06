"""LangGraph agent builder — assembles the state graph.

Graph topology (cyclic refinement):

                          ┌──────────────┐
                          │    START     │
                          └──────┬───────┘
                                 │
                          ┌──────▼───────┐
                          │  guardrail   │──── (block) ───→ output ──→ END
                          └──────┬───────┘
                                 │ (pass)
                          ┌──────▼───────┐
                          │   memory     │
                          └──────┬───────┘
                                 │
                          ┌──────▼───────┐
                          │  retriever   │  ← Milvus semantic tool search
                          └──────┬───────┘
                                 │
                    ┌────────────▼────────────┐
              ┌─────│        router          │──────┬──────────┐
              │     └────────────┬────────────┘      │          │
              │                  │                   │          │
        ┌─────▼──┐        ┌─────▼──┐         ┌──────▼───┐ ┌────▼─────┐
        │tool_exec│        │  chat  │         │  output  │ │web_search│
        └────┬────┘        └────┬───┘         └──────────┘ └────┬─────┘
             │                  │                               │
             │                  │                         ┌─────▼─────┐
             │                  │                         │ web_fetch │
             │                  │                         └─────┬─────┘
             │                  │                               │
             └──────────────────┴───────────────────────────────┘
                                │
                         ┌──────▼───────┐
                         │   compose    │  ← LLM completeness check
                         └──────┬───────┘
                                │
                     ┌──────────▼──────────┐
                     │  _should_we_finish  │
                     └───┬────────────┬────┘
                         │            │
                (finish) │            │ (continue, max 3)
                         │            │
                  ┌──────▼───────┐    │
                  │   output     │    └──→ router (loop back)
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
    tool_retriever_node,
    web_fetch_node,
    web_search_node,
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

    # Tool retrieval (Milvus semantic search)
    retrieved_tools: Annotated[list[dict], _replace_value]

    # Tool execution
    tool_results: Annotated[list[ToolResult], _replace_value]
    artifacts: Annotated[list[dict], _replace_value]

    # Web search
    web_search_results: Annotated[list[dict], _replace_value]
    web_fetch_content: Annotated[str, _replace_value]

    # Guardrails
    input_guardrail: Annotated[GuardrailResult | None, _replace_value]
    output_guardrail: Annotated[GuardrailResult | None, _replace_value]

    # Output
    final_answer: Annotated[str, _replace_value]
    tool_calls_log: Annotated[list[dict], _replace_value]

    # Control
    next_step: Annotated[str, _replace_value]
    iteration: Annotated[int, _replace_value]
    max_iterations: Annotated[int, _replace_value]
    is_complete: Annotated[bool, _replace_value]
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
        if decision == "web_search":
            return "web_search"
        if decision == "memory":
            return "chat"
        if decision == "clarify":
            return "chat"
    return "chat"


def _route_after_tool_exec(state: dict) -> str:
    """After tool execution, always compose the final answer."""
    return "compose"


_MAX_REFINEMENT_ITERATIONS = 3


def _should_we_finish(state: dict) -> str:
    """Decide whether the compose result is complete or needs another loop.

    Returns 'finish' → output, 'continue' → router for more data.
    Hard cap at _MAX_REFINEMENT_ITERATIONS to prevent infinite loops.
    """
    iteration = state.get("iteration", 1)
    if iteration >= _MAX_REFINEMENT_ITERATIONS:
        logger.info("Refinement cap reached (iteration=%d), finishing", iteration)
        return "finish"

    if state.get("is_complete", True):
        return "finish"

    return "continue"


def build_agent_graph() -> StateGraph:
    """Build and compile the LangGraph agent with cyclic refinement."""
    workflow = StateGraph(GraphState)

    # 1. Добавление узлов
    workflow.add_node("guardrail", input_guardrail_node)
    workflow.add_node("memory", memory_node)
    workflow.add_node("retriever", tool_retriever_node)
    workflow.add_node("router", router_node)
    workflow.add_node("tool_exec", tool_execution_node)
    workflow.add_node("chat", chat_node)
    workflow.add_node("web_search", web_search_node)
    workflow.add_node("web_fetch", web_fetch_node)
    workflow.add_node("compose", compose_node)
    workflow.add_node("output", output_node)

    # 2. Точка входа
    workflow.set_entry_point("guardrail")

    # 3. Переходы
    workflow.add_conditional_edges("guardrail", _route_after_guardrail, {
        "memory": "memory",
        "output": "output",
    })

    workflow.add_edge("memory", "retriever")
    workflow.add_edge("retriever", "router")

    # Роутер теперь направляет в действия
    workflow.add_conditional_edges("router", _route_after_router, {
        "tool_exec": "tool_exec",
        "chat": "chat",
        "web_search": "web_search",
        "output": "output",
    })

    # Путь инструментов и веб-поиска теперь ВСЕГДА ведет в compose
    workflow.add_edge("tool_exec", "compose")
    workflow.add_edge("web_search", "web_fetch")
    workflow.add_edge("web_fetch", "compose")
    workflow.add_edge("chat", "compose") # Чат тоже синтезируется для единообразия

    # 4. Циклическая проверка: возвращаемся в роутер или идем в output?
    # Вам нужно реализовать функцию _should_we_finish(state)
    workflow.add_conditional_edges("compose", _should_we_finish, {
        "finish": "output",
        "continue": "router", 
    })

    workflow.add_edge("output", END)

    return workflow.compile()


# Singleton compiled graph
agent_graph = build_agent_graph()
