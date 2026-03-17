"""Type definitions for router node pipeline.

These types provide explicit contracts for router pipeline stages.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.graph.orchestration_types import next_step_from_router_decision, RouterNodeUpdate
from app.schemas.graph import RouterOutput


@dataclass
class RouterInput:
    """Extracted and validated inputs for router pipeline."""
    user_message: str
    history: list[dict] = field(default_factory=list)
    feedback_plan: str = ""
    retrieved_tools: list[dict] = field(default_factory=list)
    
    # Context for tool selection
    user_id: Any = None
    session_id: Any = None
    
    @property
    def has_feedback(self) -> bool:
        """Check if we have feedback from previous iteration."""
        return bool(self.feedback_plan.strip())


@dataclass
class RouterResult:
    """Result from router pipeline."""
    output: RouterOutput | None = None
    error: str = ""
    
    @property
    def is_success(self) -> bool:
        """Check if routing succeeded."""
        return self.output is not None
    
    def to_state_update(self) -> dict[str, Any]:
        """Convert to state update dict for LangGraph."""
        if self.output is None:
            return {}
        return RouterNodeUpdate(
            router_output=self.output,
            next_step=next_step_from_router_decision(self.output.decision),
        ).to_state_update()
    
    @classmethod
    def success(cls, output: RouterOutput) -> RouterResult:
        """Create a successful routing result."""
        return cls(output=output, error="")
    
    @classmethod
    def failure(cls, error: str) -> RouterResult:
        """Create a failed routing result."""
        return cls(output=None, error=error)
