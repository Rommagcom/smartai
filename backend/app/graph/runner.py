from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

GraphResult = dict[str, Any]
GraphShortCircuit = tuple[str, list[str], list[str], list[dict], list[dict]]


async def invoke_agent_graph_with_recovery(
    initial_state: dict[str, Any],
    on_graph_failure: Callable[[Exception], Awaitable[GraphShortCircuit]],
) -> tuple[GraphResult | None, GraphShortCircuit | None]:
    """Run LangGraph and delegate exception recovery to caller-provided callback.

    Returns (graph_result, short_circuit_response). Exactly one of these values
    is expected to be non-None.
    """
    from app.graph import agent_graph

    try:
        result = await agent_graph.ainvoke(initial_state)
        return result, None
    except Exception as exc:
        logger.exception("LangGraph invocation failed in runner")
        short_circuit = await on_graph_failure(exc)
        return None, short_circuit
