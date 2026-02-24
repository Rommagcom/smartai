from __future__ import annotations

from enum import StrEnum


class WorkerJobType(StrEnum):
    WEB_SEARCH = "web_search"
    WEB_FETCH = "web_fetch"
    PDF_CREATE = "pdf_create"


class WorkerJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_SCHEDULED = "retry_scheduled"
    SUCCESS = "success"
    FAILED = "failed"
