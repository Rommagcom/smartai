"""Type definitions for compose node pipeline.

These types provide explicit contracts for each pipeline stage,
making the data flow through compose_node clear and testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.graph.orchestration_types import ComposeNodeUpdate
from app.schemas.graph import ToolResult


@dataclass
class ComposeInput:
    """Extracted and validated inputs for compose pipeline.
    
    Represents all data needed by compose stages: context, results, state.
    """
    # Message & query
    user_message: str
    history: list[dict]  # Chat history
    
    # Results from previous nodes
    tool_results: list[ToolResult] = field(default_factory=list)
    web_fetch_content: str = ""
    web_search_results: list[dict] = field(default_factory=list)
    existing_answer: str = ""
    
    # Context layers
    state_context: list[str] = field(default_factory=list)
    
    # Iteration tracking
    iterations: int = 1
    max_iterations: int = 3
    
    # Derived flags
    all_failed: bool = True  # True if all tools failed
    has_integration: bool = False  # True if any tool is integration_call
    
    @property
    def has_web_context(self) -> bool:
        """Check if we have web results to work with."""
        return bool(self.web_fetch_content or self.web_search_results)
    
    @property
    def has_tool_results(self) -> bool:
        """Check if we have tool execution results."""
        return bool(self.tool_results)
    
    @property
    def has_any_results(self) -> bool:
        """Check if we have ANY results (tools, web, or existing answer)."""
        return self.has_tool_results or self.has_web_context or bool(self.existing_answer)


@dataclass
class ComposeResult:
    """Final result from compose pipeline.
    
    Either a complete answer ready for output, or feedback plan
    for next iteration (reflexive loop).
    """
    final_answer: str = ""
    is_complete: bool = True
    feedback_plan: str = ""
    iterations: int = 1
    iteration: int = 1  # Alias for iterations
    
    def to_state_update(self) -> dict[str, Any]:
        """Convert to state update dict for LangGraph."""
        return ComposeNodeUpdate(
            final_answer=self.final_answer,
            is_complete=self.is_complete,
            feedback_plan=self.feedback_plan,
            iterations=self.iterations,
            iteration=self.iteration,
        ).to_state_update()
    
    @classmethod
    def complete(cls, answer: str, iterations: int = 1) -> ComposeResult:
        """Create a complete result (ready for output)."""
        return cls(
            final_answer=answer,
            is_complete=True,
            feedback_plan="",
            iterations=iterations,
            iteration=iterations,
        )
    
    @classmethod
    def incomplete(cls, feedback_plan: str, iterations: int = 1) -> ComposeResult:
        """Create an incomplete result (needs another iteration)."""
        return cls(
            final_answer="",
            is_complete=False,
            feedback_plan=feedback_plan or "Нужен дополнительный поиск данных: уточнить недостающие факты и источники.",
            iterations=iterations,
            iteration=iterations,
        )
