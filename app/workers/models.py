from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field


class WorkerJobType(StrEnum):
    WEB_SEARCH = "web_search"
    WEB_FETCH = "web_fetch"
    PDF_CREATE = "pdf_create"


class WorkerJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class WorkerJob(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    job_type: WorkerJobType
    payload: dict = Field(default_factory=dict)
    status: WorkerJobStatus = WorkerJobStatus.QUEUED
    result: dict | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def mark_running(self) -> None:
        self.status = WorkerJobStatus.RUNNING
        self.updated_at = datetime.now(timezone.utc)

    def mark_success(self, result: dict) -> None:
        self.status = WorkerJobStatus.SUCCESS
        self.result = result
        self.error = None
        self.updated_at = datetime.now(timezone.utc)

    def mark_failed(self, error: str) -> None:
        self.status = WorkerJobStatus.FAILED
        self.error = error
        self.updated_at = datetime.now(timezone.utc)
