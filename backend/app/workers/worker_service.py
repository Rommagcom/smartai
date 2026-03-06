from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from time import perf_counter
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError, ReadOnlyError
from sqlalchemy import select

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.worker_task import WorkerTask
from app.services.alerting_service import alerting_service
from app.services.pdf_service import pdf_service
from app.services.delivery_format_service import build_worker_delivery_payload
from app.services.observability_metrics_service import observability_metrics_service
from app.services.websocket_manager import connection_manager
from app.services.worker_result_service import worker_result_service
from app.workers.models import WorkerJobStatus, WorkerJobType

WorkerHandler = Callable[[dict], Awaitable[dict]]
logger = logging.getLogger(__name__)


class WorkerService:
    def __init__(self) -> None:
        self._redis: Redis | None = None
        self._handlers: dict[WorkerJobType, WorkerHandler] = {
            WorkerJobType.PDF_CREATE: self._handle_pdf_create,
            WorkerJobType.EXCEL_CREATE: self._handle_excel_create,
        }

    def register_handler(self, job_type: WorkerJobType, handler: WorkerHandler) -> None:
        self._handlers[job_type] = handler

    def _get_redis(self) -> Redis:
        if self._redis is None:
            self._redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        return self._redis

    async def enqueue(
        self,
        job_type: WorkerJobType,
        payload: dict,
        *,
        max_retries: int | None = None,
        dedupe_key: str | None = None,
        priority: str | None = None,
    ) -> dict:
        started_at = perf_counter()
        success = False
        try:
            retries = max(0, max_retries if max_retries is not None else settings.WORKER_MAX_RETRIES)
            payload_copy = dict(payload or {})
            user_id = str(payload_copy.get("__user_id") or "").strip() or None
            normalized_priority = self._normalize_priority(priority or payload_copy.get("__priority"))
            payload_copy["__priority"] = normalized_priority

            dedupe = dedupe_key or self._build_dedupe_key(job_type=job_type, payload=payload_copy)
            existing_task = await self._find_deduplicated_task(dedupe)
            if existing_task:
                success = True
                return {
                    "task": existing_task,
                    "enqueued": False,
                    "deduplicated": True,
                }

            async with AsyncSessionLocal() as db:
                task = WorkerTask(
                    user_id=UUID(user_id) if user_id else None,
                    job_type=job_type.value,
                    payload=payload_copy,
                    status=WorkerJobStatus.QUEUED.value,
                    attempt_count=0,
                    max_retries=retries,
                    dedupe_key=dedupe,
                )
                db.add(task)
                await db.commit()
                await db.refresh(task)

            redis = self._get_redis()
            await redis.lpush(self._queue_key_for_priority(normalized_priority), str(task.id))
            success = True
            logger.info(
                "worker task enqueued",
                extra={
                    "context": {
                        "component": "worker",
                        "event": "enqueue",
                        "job_type": job_type.value,
                        "task_id": str(task.id),
                        "deduplicated": False,
                    }
                },
            )
            return {
                "task": task,
                "enqueued": True,
                "deduplicated": False,
            }
        finally:
            observability_metrics_service.record(
                component="worker",
                operation="enqueue",
                success=success,
                latency_ms=(perf_counter() - started_at) * 1000,
            )
        
        
    

    async def get_job(self, job_id: str) -> WorkerTask | None:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(WorkerTask).where(WorkerTask.id == UUID(job_id)))
            return result.scalar_one_or_none()

    async def run_once(self) -> WorkerTask | None:
        await self._recover_processing_queue()
        await self._recover_stale_running_tasks()
        await self._promote_retries()
        redis = self._get_redis()
        task_id = await redis.brpoplpush(
            settings.WORKER_QUEUE_HIGH_KEY,
            settings.WORKER_PROCESSING_QUEUE_KEY,
            timeout=1,
        )
        if not task_id:
            task_id = await redis.brpoplpush(
                settings.WORKER_QUEUE_KEY,
                settings.WORKER_PROCESSING_QUEUE_KEY,
                timeout=settings.WORKER_BRPOP_TIMEOUT_SECONDS,
            )
        if not task_id:
            return None
        return await self._process_task(task_id)

    def _reset_redis(self) -> None:
        """Drop cached Redis connection so next `_get_redis()` reconnects."""
        old = self._redis
        self._redis = None
        if old is not None:
            try:
                asyncio.get_running_loop().create_task(old.aclose())
            except Exception:  # noqa: B110
                pass

    async def run_forever(self) -> None:
        redis_backoff = 0.0
        _REDIS_BACKOFF_MAX = 30.0

        while True:
            try:
                task = await self.run_once()
                redis_backoff = 0.0          # healthy cycle — reset backoff
                if task is None:
                    await asyncio.sleep(0.2)
            except (ReadOnlyError, RedisConnectionError, OSError) as exc:
                # Redis is unreachable or in read-only state (replica / failover).
                # Use exponential backoff and force a fresh connection.
                self._reset_redis()
                redis_backoff = min(
                    _REDIS_BACKOFF_MAX,
                    max(1.0, redis_backoff * 2) if redis_backoff else 1.0,
                )
                alerting_service.emit(
                    component="worker",
                    severity="critical",
                    message="Redis unavailable — retrying",
                    details={"error": str(exc), "backoff_s": redis_backoff},
                )
                logger.error(
                    "worker redis error, backoff=%.1fs: %s",
                    redis_backoff,
                    exc,
                )
                await asyncio.sleep(redis_backoff)
            except Exception as exc:
                alerting_service.emit(
                    component="worker",
                    severity="critical",
                    message="worker loop crashed",
                    details={"error": str(exc)},
                )
                logger.exception("worker loop error")
                await asyncio.sleep(1.0)

    async def _process_task(self, task_id: str) -> WorkerTask | None:
        started_at = perf_counter()
        success = False
        redis = self._get_redis()
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(WorkerTask).where(WorkerTask.id == UUID(task_id)))
                task = result.scalar_one_or_none()
                if not task:
                    return None

                task.status = WorkerJobStatus.RUNNING.value
                task.started_at = datetime.now(timezone.utc)
                task.next_retry_at = None
                await db.commit()

                try:
                    job_type = WorkerJobType(task.job_type)
                    handler = self._handlers.get(job_type)
                except Exception:
                    handler = None

                if not handler:
                    await self._fail_task(db, task, f"No handler for job_type={task.job_type}")
                    observability_metrics_service.record(
                        component="worker",
                        operation="process_task",
                        success=False,
                        latency_ms=(perf_counter() - started_at) * 1000,
                    )
                    return task

                try:
                    run_result = await handler(task.payload)
                    task.status = WorkerJobStatus.SUCCESS.value
                    task.result = run_result
                    task.error = None
                    task.completed_at = datetime.now(timezone.utc)
                    await db.commit()
                    await self._notify_user(task)
                    success = True
                    logger.info(
                        "worker task finished",
                        extra={
                            "context": {
                                "component": "worker",
                                "event": "task_success",
                                "task_id": str(task.id),
                                "job_type": task.job_type,
                            }
                        },
                    )
                except Exception as exc:
                    await self._fail_task(db, task, str(exc))

                observability_metrics_service.record(
                    component="worker",
                    operation="process_task",
                    success=success,
                    latency_ms=(perf_counter() - started_at) * 1000,
                )
                return task
        finally:
            await redis.lrem(settings.WORKER_PROCESSING_QUEUE_KEY, 0, task_id)

    async def _fail_task(self, db, task: WorkerTask, error: str) -> None:
        task.attempt_count = int(task.attempt_count) + 1
        task.error = error

        if task.attempt_count <= task.max_retries:
            delay = self._retry_delay_seconds(task.attempt_count)
            run_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            task.status = WorkerJobStatus.RETRY_SCHEDULED.value
            task.next_retry_at = run_at
            await db.commit()

            redis = self._get_redis()
            await redis.zadd(settings.WORKER_RETRY_ZSET_KEY, {str(task.id): run_at.timestamp()})
            return

        task.status = WorkerJobStatus.FAILED.value
        task.completed_at = datetime.now(timezone.utc)
        await db.commit()
        alerting_service.emit(
            component="worker",
            severity="warning",
            message="worker task failed permanently",
            details={
                "task_id": str(task.id),
                "job_type": task.job_type,
                "attempt_count": int(task.attempt_count),
                "error": str(error),
            },
        )
        await self._notify_user(task)

    async def _promote_retries(self) -> None:
        redis = self._get_redis()
        now_ts = datetime.now(timezone.utc).timestamp()
        ready_ids = await redis.zrangebyscore(settings.WORKER_RETRY_ZSET_KEY, min=0, max=now_ts, start=0, num=100)
        if not ready_ids:
            return

        async with AsyncSessionLocal() as db:
            uuids = [UUID(task_id) for task_id in ready_ids]
            result = await db.execute(select(WorkerTask).where(WorkerTask.id.in_(uuids)))
            rows = result.scalars().all()
            for row in rows:
                row.status = WorkerJobStatus.QUEUED.value
                row.next_retry_at = None
            await db.commit()

        pipe = redis.pipeline()
        priority_by_task_id = {
            str(row.id): self._normalize_priority((row.payload or {}).get("__priority"))
            for row in rows
        }
        for task_id in ready_ids:
            pipe.zrem(settings.WORKER_RETRY_ZSET_KEY, task_id)
            pipe.lpush(self._queue_key_for_priority(priority_by_task_id.get(task_id)), task_id)
        await pipe.execute()

    async def _recover_processing_queue(self) -> None:
        redis = self._get_redis()
        max_batch = max(10, int(settings.WORKER_PROCESSING_RECOVERY_BATCH))
        raw_ids = await redis.lrange(settings.WORKER_PROCESSING_QUEUE_KEY, 0, max_batch - 1)
        task_ids = [str(item) for item in raw_ids if str(item).strip()]
        if not task_ids:
            return

        valid_uuids: list[UUID] = []
        for task_id in task_ids:
            try:
                valid_uuids.append(UUID(task_id))
            except ValueError:
                await redis.lrem(settings.WORKER_PROCESSING_QUEUE_KEY, 0, task_id)

        if not valid_uuids:
            return

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(WorkerTask).where(WorkerTask.id.in_(valid_uuids)))
            rows = result.scalars().all()
            task_map = {str(row.id): row for row in rows}

            now = datetime.now(timezone.utc)
            lease_seconds = max(10, int(settings.WORKER_RUNNING_LEASE_SECONDS))
            stale_before = now - timedelta(seconds=lease_seconds)

            for task_id in task_ids:
                task = task_map.get(task_id)
                await self._recover_processing_item(
                    db=db,
                    redis=redis,
                    task_id=task_id,
                    task=task,
                    stale_before=stale_before,
                )

    async def _recover_processing_item(
        self,
        *,
        db,
        redis: Redis,
        task_id: str,
        task: WorkerTask | None,
        stale_before: datetime,
    ) -> None:
        if not task:
            await redis.lrem(settings.WORKER_PROCESSING_QUEUE_KEY, 0, task_id)
            return

        status = str(task.status or "")
        terminal_statuses = {
            WorkerJobStatus.SUCCESS.value,
            WorkerJobStatus.FAILED.value,
            WorkerJobStatus.RETRY_SCHEDULED.value,
        }
        if status in terminal_statuses:
            await redis.lrem(settings.WORKER_PROCESSING_QUEUE_KEY, 0, task_id)
            return

        if status == WorkerJobStatus.QUEUED.value:
            priority = self._normalize_priority((task.payload or {}).get("__priority"))
            await redis.lpush(self._queue_key_for_priority(priority), task_id)
            await redis.lrem(settings.WORKER_PROCESSING_QUEUE_KEY, 0, task_id)
            return

        is_stale_running = status == WorkerJobStatus.RUNNING.value and task.started_at and task.started_at < stale_before
        if is_stale_running:
            await self._fail_task(db, task, "Worker lease timeout: recovered stale RUNNING task")
            await redis.lrem(settings.WORKER_PROCESSING_QUEUE_KEY, 0, task_id)

    async def _recover_stale_running_tasks(self) -> None:
        now = datetime.now(timezone.utc)
        lease_seconds = max(10, int(settings.WORKER_RUNNING_LEASE_SECONDS))
        stale_before = now - timedelta(seconds=lease_seconds)

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(WorkerTask).where(
                    WorkerTask.status == WorkerJobStatus.RUNNING.value,
                    WorkerTask.started_at.is_not(None),
                    WorkerTask.started_at < stale_before,
                )
            )
            rows = result.scalars().all()
            for row in rows:
                await self._fail_task(db, row, "Worker lease timeout: recovered stale RUNNING task")

    @staticmethod
    def _retry_delay_seconds(attempt_count: int) -> int:
        base = max(1, settings.WORKER_RETRY_BASE_DELAY_SECONDS)
        max_delay = max(base, settings.WORKER_RETRY_MAX_DELAY_SECONDS)
        value = base * (2 ** max(0, attempt_count - 1))
        return min(max_delay, value)

    @staticmethod
    def _build_dedupe_key(job_type: WorkerJobType, payload: dict) -> str:
        user_id = str(payload.get("__user_id") or "")
        normalized_payload = {
            key: value
            for key, value in (payload or {}).items()
            if not key.startswith("__")
        }
        raw = json.dumps(
            {
                "job_type": job_type.value,
                "user_id": user_id,
                "payload": normalized_payload,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_priority(priority: object) -> str:
        lowered = str(priority or "").strip().lower()
        if lowered in {"high", "urgent", "interactive"}:
            return "high"
        return "normal"

    @staticmethod
    def _queue_key_for_priority(priority: object) -> str:
        normalized = str(priority or "").strip().lower()
        if normalized == "high":
            return settings.WORKER_QUEUE_HIGH_KEY
        return settings.WORKER_QUEUE_KEY

    async def _find_deduplicated_task(self, dedupe_key: str) -> WorkerTask | None:
        window_start = datetime.now(timezone.utc) - timedelta(seconds=settings.WORKER_DEDUPE_WINDOW_SECONDS)
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(WorkerTask)
                .where(
                    WorkerTask.dedupe_key == dedupe_key,
                    WorkerTask.status.in_(
                        [
                            WorkerJobStatus.QUEUED.value,
                            WorkerJobStatus.RUNNING.value,
                            WorkerJobStatus.RETRY_SCHEDULED.value,
                        ]
                    ),
                    WorkerTask.created_at >= window_start,
                )
                .order_by(WorkerTask.created_at.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def _handle_pdf_create(self, payload: dict) -> dict:
        title = str(payload.get("title") or "Generated document")
        raw_content = str(payload.get("content") or "").strip()
        filename = str(payload.get("filename") or "document.pdf")
        if not raw_content:
            raise ValueError("pdf_create job requires content")
        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"

        # Expand prompt-like content via LLM before generating PDF
        try:
            from app.services.tool_orchestrator_service import ToolOrchestratorService
            content = await ToolOrchestratorService._maybe_summarize_content(raw_content, title)
            if not content:
                content = raw_content
        except Exception:
            logger.warning("worker pdf content expansion failed, using raw", exc_info=True)
            content = raw_content

        return await asyncio.to_thread(pdf_service.create_pdf_base64, title, content, filename)

    async def _handle_excel_create(self, payload: dict) -> dict:
        from app.services.excel_service import excel_service

        title = str(payload.get("title") or "Generated document")
        raw_content = str(payload.get("content") or "").strip()
        filename = str(payload.get("filename") or "document.xlsx")
        if not raw_content:
            raise ValueError("excel_create job requires content")
        if not filename.lower().endswith(".xlsx"):
            filename = f"{filename}.xlsx"

        # Expand prompt-like content via LLM before generating Excel
        try:
            from app.services.tool_orchestrator_service import ToolOrchestratorService
            content = await ToolOrchestratorService._maybe_summarize_content(raw_content, title)
            if not content:
                content = raw_content
        except Exception:
            logger.warning("worker excel content expansion failed, using raw", exc_info=True)
            content = raw_content

        columns = payload.get("columns")
        rows = payload.get("rows")
        return await asyncio.to_thread(
            excel_service.create_excel_base64,
            title,
            content,
            filename,
            columns if isinstance(columns, list) else None,
            rows if isinstance(rows, list) else None,
        )

    async def _notify_user(self, job: WorkerTask) -> None:
        payload_data = job.payload if isinstance(job.payload, dict) else {}
        user_id = str(payload_data.get("__user_id") or (job.user_id and str(job.user_id)) or "").strip()
        if not user_id:
            return

        is_success = job.status == WorkerJobStatus.SUCCESS.value
        payload = build_worker_delivery_payload(
            job_type=job.job_type,
            is_success=is_success,
            result=job.result,
            error_message=job.error,
        )
        await worker_result_service.push(user_id=user_id, payload=payload)
        await connection_manager.send_to_user(user_id=user_id, payload=payload)


worker_service = WorkerService()
