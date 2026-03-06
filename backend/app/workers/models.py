from __future__ import annotations

from enum import StrEnum


class WorkerJobType(StrEnum):
    PDF_CREATE = "pdf_create"
    EXCEL_CREATE = "excel_create"


class WorkerJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_SCHEDULED = "retry_scheduled"
    SUCCESS = "success"
    FAILED = "failed"
