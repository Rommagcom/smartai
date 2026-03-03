from pydantic import BaseModel


class DocumentDeleteResponse(BaseModel):
    status: str
    deleted_count: int
    source_doc: str | None = None


class DocumentItem(BaseModel):
    source_doc: str
    chunks: int


class DocumentListResponse(BaseModel):
    items: list[DocumentItem]
