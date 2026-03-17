"""Type definitions for output node pipeline."""
from dataclasses import dataclass, field
from typing import Any

from app.graph.orchestration_types import OutputNodeUpdate


@dataclass
class OutputInput:
    """Input container for output node pipeline._dev_log
    
    Contains all state variables needed for output processing:
    - final_answer: The answer to be finalized
    - user_message: Original user question
    - user_id: User identifier for memory operations
    - web context: web_fetch_content, web_search_results
    - tool_results: Results from tool execution
    - existing state: existing tool_calls_log, artifacts, tool_calls_log
    """
    
    final_answer: str
    user_message: str
    user_id: Any
    web_fetch_content: str
    web_search_results: list[dict]
    tool_results: list[Any]
    existing_calls: list[dict]
    existing_artifacts: list[dict]
    
    @property
    def has_web_context(self) -> bool:
        """Check if web context exists."""
        return bool(self.web_fetch_content or self.web_search_results)
    
    @property
    def has_export_context(self) -> bool:
        """Check if any export-relevant state exists."""
        return bool(self.user_id and self.final_answer)


@dataclass
class OutputResult:
    """Result container for output node pipeline.
    
    Contains final state updates to be returned from output_node.
    """
    
    final_answer: str
    output_guardrail: tuple[bool, str]
    tool_calls_log: list[dict]
    artifacts: list[dict]
    
    def to_state_update(self) -> dict:
        """Convert result to state update dict."""
        return OutputNodeUpdate(
            final_answer=self.final_answer,
            output_guardrail=self.output_guardrail,
            tool_calls_log=self.tool_calls_log,
            artifacts=self.artifacts,
        ).to_state_update()
