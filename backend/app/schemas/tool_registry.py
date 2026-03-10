"""Pydantic v2 schemas for the Tool Vector Registry & register_api_tool flow.

These schemas define:
- ``RegisterApiToolInput`` — what the user provides (via natural language → LLM)
- ``ToolVectorRecord`` — what gets stored in Milvus
- ``RetrievedTool`` — what the retriever node returns to the planner
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RegisterApiToolInput(BaseModel):
    """Schema that the LLM fills when the user describes a new API tool.

    The router uses ``register_api_tool`` skill, and the LLM generates
    this model from a natural-language description of an API.
    """
    tool_name: str = Field(..., description="Unique tool name (latin, snake_case, 2-63 chars)")
    description: str = Field(..., description="Human-readable description of what the tool does")
    api_endpoint: str = Field(..., description="Full URL of the API endpoint")
    method: str = Field(default="GET", description="HTTP method: GET, POST, PUT, PATCH, DELETE")
    headers: dict[str, str] = Field(default_factory=dict, description="Custom HTTP headers")
    auth_token: str | None = Field(default=None, description="Bearer token if required")
    parameters_schema: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema for API parameters ({type: object, properties: {...}, required: [...]})",
    )
    response_hint: str = Field(
        default="",
        description="Brief description of the expected response format",
    )


class ToolVectorRecord(BaseModel):
    """Represents a tool stored in the Milvus ``tool_vectors`` collection."""
    tool_name: str
    user_id: str
    tool_type: str = Field(description="'dynamic' | 'integration' | 'builtin'")
    description: str
    endpoint: str = ""
    method: str = "GET"
    parameters_schema: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievedTool(BaseModel):
    """A tool result from the Milvus semantic search (retriever output)."""
    score: float = Field(description="Cosine similarity score (0..1)")
    tool_name: str
    tool_type: str
    description: str
    endpoint: str = ""
    method: str = "GET"
    parameters_schema: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_planner_signature(self) -> str:
        """Format as a planner-compatible signature string."""
        params = []
        props = self.parameters_schema.get("properties", {})
        for pname in props:
            params.append(pname)
        param_str = ", ".join(params) if params else ""
        prefix = ""
        if self.tool_type == "dynamic":
            prefix = "dyn:"
        elif self.tool_type == "integration":
            prefix = "integ:"
        sig = f"{prefix}{self.tool_name}({param_str})"
        desc = self.description[:80] if self.description else ""
        return f"{sig} — {desc}" if desc else sig
