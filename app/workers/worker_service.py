from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.services.pdf_service import pdf_service
from app.services.web_tools_service import web_tools_service
from app.services.websocket_manager import connection_manager
from app.services.worker_result_service import worker_result_service
from app.workers.models import WorkerJob, WorkerJobType

WorkerHandler = Callable[[dict], Awaitable[dict]]


class WorkerService:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[WorkerJob] = asyncio.Queue()
        self._jobs: dict[str, WorkerJob] = {}
        self._handlers: dict[WorkerJobType, WorkerHandler] = {
            WorkerJobType.WEB_SEARCH: self._handle_web_search,
            WorkerJobType.WEB_FETCH: self._handle_web_fetch,
            WorkerJobType.PDF_CREATE: self._handle_pdf_create,
        }

    def register_handler(self, job_type: WorkerJobType, handler: WorkerHandler) -> None:
        self._handlers[job_type] = handler

    async def enqueue(self, job_type: WorkerJobType, payload: dict) -> WorkerJob:
        job = WorkerJob(job_type=job_type, payload=payload)
        self._jobs[job.id] = job
        await self._queue.put(job)
        return job

    def get_job(self, job_id: str) -> WorkerJob | None:
        return self._jobs.get(job_id)

    async def run_once(self) -> WorkerJob:
        job = await self._queue.get()
        job.mark_running()
        handler = self._handlers.get(job.job_type)
        if not handler:
            job.mark_failed(f"No handler for job_type={job.job_type}")
            await self._notify_user(job)
            return job

        try:
            result = await handler(job.payload)
            job.mark_success(result=result)
        except Exception as exc:
            job.mark_failed(str(exc))
        await self._notify_user(job)
        return job

    async def run_forever(self) -> None:
        while True:
            await self.run_once()

    async def _handle_web_search(self, payload: dict) -> dict:
        query = str(payload.get("query") or "").strip()
        if not query:
            raise ValueError("web_search job requires query")
        limit = int(payload.get("limit", 5))
        return await web_tools_service.web_search(query=query, limit=max(1, min(limit, 10)))

    async def _handle_web_fetch(self, payload: dict) -> dict:
        url = str(payload.get("url") or "").strip()
        if not url:
            raise ValueError("web_fetch job requires url")
        max_chars = int(payload.get("max_chars", 12000))
        return await web_tools_service.web_fetch(url=url, max_chars=max(1000, min(max_chars, 50000)))

    async def _handle_pdf_create(self, payload: dict) -> dict:
        title = str(payload.get("title") or "Generated document")
        content = str(payload.get("content") or "").strip()
        filename = str(payload.get("filename") or "document.pdf")
        if not content:
            raise ValueError("pdf_create job requires content")
        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"
        return await asyncio.to_thread(pdf_service.create_pdf_base64, title, content, filename)

    async def _notify_user(self, job: WorkerJob) -> None:
        user_id = str(job.payload.get("__user_id") or "").strip()
        if not user_id:
            return

        if job.status.value == "success":
            payload = {
                "type": "worker_result",
                "status": "success",
                "job_type": job.job_type.value,
                "message": "Фоновая задача выполнена.",
                "result": self._result_preview(job.result),
            }
        else:
            payload = {
                "type": "worker_result",
                "status": "failed",
                "job_type": job.job_type.value,
                "message": "Фоновая задача завершилась с ошибкой.",
                "error": job.error,
            }
        worker_result_service.push(user_id=user_id, payload=payload)
        await connection_manager.send_to_user(user_id=user_id, payload=payload)

    @staticmethod
    def _result_preview(result: dict | None) -> dict:
        if not isinstance(result, dict):
            return {"raw": str(result)}

        preview = dict(result)
        file_base64 = preview.pop("file_base64", None)
        if file_base64 is not None:
            preview["artifact_ready"] = True
            preview["artifact_note"] = "Результат содержит файл. Для передачи файла используйте чатовый tool-вызов без фоновой очереди."
        return preview


worker_service = WorkerService()
