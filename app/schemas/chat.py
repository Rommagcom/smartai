from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str
    session_id: UUID | None = None


class ChatResponse(BaseModel):
    session_id: UUID
    response: str
    used_memory_ids: list[UUID] = []
    tool_calls: list[dict] = []
    artifacts: list[dict] = []


class FeedbackRequest(BaseModel):
    message_id: UUID
    feedback_score: int


class MessageOut(BaseModel):
    id: UUID
    role: str
    content: str
    metadata: dict = Field(validation_alias="meta", serialization_alias="metadata")
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class WebSearchRequest(BaseModel):
    query: str
    limit: int = 5


class WebFetchRequest(BaseModel):
    url: str
    max_chars: int = 12000


class BrowserActionRequest(BaseModel):
    url: str
    action: str = "extract_text"
    max_chars: int = 8000
    timeout_seconds: int = 30


class PdfCreateRequest(BaseModel):
    title: str = "Generated document"
    content: str
    filename: str = "document.pdf"
