from datetime import datetime, timezone
import logging
from time import perf_counter

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.cron_job import CronJob
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.services.alerting_service import alerting_service
from app.services.delivery_format_service import build_worker_delivery_payload
from app.services.observability_metrics_service import observability_metrics_service
from app.services.websocket_manager import connection_manager
from app.services.worker_result_service import worker_result_service

logger = logging.getLogger(__name__)


class SchedulerService:
    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler(timezone="UTC")

    def start(self) -> None:
        started_at = perf_counter()
        success = False
        try:
            if not self.scheduler.running:
                self.scheduler.start()
                self.scheduler.add_job(self.periodic_proactive_ping, "interval", minutes=30, id="global_proactive_ping", replace_existing=True)
                self.scheduler.add_job(self.sync_jobs_from_db, "interval", seconds=30, id="global_cron_sync", replace_existing=True)
                logger.info("scheduler started", extra={"context": {"component": "scheduler", "event": "start"}})
            success = True
        except Exception as exc:
            alerting_service.emit(component="scheduler", severity="critical", message="scheduler start failed", details={"error": str(exc)})
            raise
        finally:
            observability_metrics_service.record(
                component="scheduler",
                operation="start",
                success=success,
                latency_ms=(perf_counter() - started_at) * 1000,
            )

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("scheduler stopped", extra={"context": {"component": "scheduler", "event": "shutdown"}})

    async def bootstrap_from_db(self) -> dict:
        started_at = perf_counter()
        success = False
        loaded = 0
        failed = 0
        removed = 0
        try:
            sync_result = await self._sync_jobs_from_db_internal(force_reload_all=True)
            loaded = int(sync_result.get("loaded", 0))
            failed = int(sync_result.get("failed", 0))
            removed = int(sync_result.get("removed", 0))
            success = failed == 0
            logger.info(
                "scheduler bootstrap complete",
                extra={
                    "context": {
                        "component": "scheduler",
                        "event": "bootstrap",
                        "loaded": loaded,
                        "failed": failed,
                        "removed": removed,
                    }
                },
            )
            return {"loaded": loaded, "failed": failed, "removed": removed}
        except Exception as exc:
            alerting_service.emit(
                component="scheduler",
                severity="critical",
                message="scheduler bootstrap failed",
                details={"error": str(exc)},
            )
            raise
        finally:
            observability_metrics_service.record(
                component="scheduler",
                operation="bootstrap",
                success=success,
                latency_ms=(perf_counter() - started_at) * 1000,
            )

    @staticmethod
    def _is_managed_cron_job_id(job_id: str) -> bool:
        return not str(job_id).startswith("global_")

    async def _sync_jobs_from_db_internal(self, force_reload_all: bool = False) -> dict:
        loaded = 0
        failed = 0
        removed = 0

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(CronJob).where(CronJob.is_active.is_(True)).order_by(CronJob.created_at.desc())
            )
            rows = result.scalars().all()

        active_ids = {str(row.id) for row in rows}
        existing_ids = {
            str(job.id)
            for job in self.scheduler.get_jobs()
            if self._is_managed_cron_job_id(str(job.id))
        }

        for row in rows:
            row_id = str(row.id)
            if not force_reload_all and row_id in existing_ids:
                continue
            try:
                self.add_or_replace_job(
                    job_id=row_id,
                    cron_expression=row.cron_expression,
                    user_id=str(row.user_id),
                    action_type=row.action_type,
                    payload=row.payload if isinstance(row.payload, dict) else {},
                )
                loaded += 1
            except Exception:
                failed += 1

        stale_ids = existing_ids - active_ids
        for stale_id in stale_ids:
            try:
                self.scheduler.remove_job(stale_id)
                removed += 1
            except Exception:
                failed += 1

        return {"loaded": loaded, "failed": failed, "removed": removed}

    async def sync_jobs_from_db(self) -> dict:
        started_at = perf_counter()
        success = False
        loaded = 0
        failed = 0
        removed = 0
        try:
            sync_result = await self._sync_jobs_from_db_internal(force_reload_all=False)
            loaded = int(sync_result.get("loaded", 0))
            failed = int(sync_result.get("failed", 0))
            removed = int(sync_result.get("removed", 0))
            success = failed == 0
            if loaded or removed or failed:
                logger.info(
                    "scheduler periodic sync",
                    extra={
                        "context": {
                            "component": "scheduler",
                            "event": "sync",
                            "loaded": loaded,
                            "removed": removed,
                            "failed": failed,
                        }
                    },
                )
            return {"loaded": loaded, "failed": failed, "removed": removed}
        except Exception as exc:
            alerting_service.emit(
                component="scheduler",
                severity="warning",
                message="scheduler periodic sync failed",
                details={"error": str(exc)},
            )
            raise
        finally:
            observability_metrics_service.record(
                component="scheduler",
                operation="sync",
                success=success,
                latency_ms=(perf_counter() - started_at) * 1000,
            )

    def add_or_replace_job(self, job_id: str, cron_expression: str, user_id: str, action_type: str, payload: dict) -> None:
        started_at = perf_counter()
        success = False
        try:
            if cron_expression.startswith("@once:"):
                run_at = datetime.fromisoformat(cron_expression.replace("@once:", "", 1))
                trigger = DateTrigger(run_date=run_at)
            else:
                trigger = CronTrigger.from_crontab(cron_expression)
            self.scheduler.add_job(
                self.execute_action,
                trigger=trigger,
                id=job_id,
                replace_existing=True,
                kwargs={"user_id": user_id, "action_type": action_type, "payload": payload},
            )
            success = True
            logger.info(
                "scheduler job added",
                extra={"context": {"component": "scheduler", "event": "job_add", "job_id": job_id, "action_type": action_type}},
            )
        except Exception as exc:
            alerting_service.emit(
                component="scheduler",
                severity="warning",
                message="scheduler add job failed",
                details={"job_id": job_id, "error": str(exc)},
            )
            raise
        finally:
            observability_metrics_service.record(
                component="scheduler",
                operation="add_job",
                success=success,
                latency_ms=(perf_counter() - started_at) * 1000,
            )

    async def execute_action(self, user_id: str, action_type: str, payload: dict) -> None:
        started_at = perf_counter()
        success = False
        now = datetime.now(timezone.utc).isoformat()
        try:
            normalized_action_type = str(action_type or "").strip().lower()
            message_action_types = {"send_message", "reminder", "notification", "daily_briefing"}

            if normalized_action_type in message_action_types:
                message_text = (
                    payload.get("message")
                    or payload.get("task_text")
                    or payload.get("text")
                    or "Напоминание от ассистента"
                )
                await connection_manager.send_to_user(
                    user_id,
                    {
                        "type": "proactive_message",
                        "message": message_text,
                        "timestamp": now,
                    },
                )
                delivery_payload = build_worker_delivery_payload(
                    job_type="cron_reminder",
                    is_success=True,
                    result={
                        "message": str(message_text),
                        "source": "scheduler",
                        "timestamp": now,
                    },
                )
                await worker_result_service.push(user_id=user_id, payload=delivery_payload)
            else:
                raise ValueError(f"Unsupported scheduler action_type: {action_type}")
            success = True
        except Exception as exc:
            alerting_service.emit(
                component="scheduler",
                severity="warning",
                message="scheduler execute action failed",
                details={"action_type": action_type, "user_id": user_id, "error": str(exc)},
            )
            raise
        finally:
            observability_metrics_service.record(
                component="scheduler",
                operation="execute_action",
                success=success,
                latency_ms=(perf_counter() - started_at) * 1000,
            )

    async def periodic_proactive_ping(self) -> None:
        started_at = perf_counter()
        success = False
        now = datetime.now(timezone.utc).isoformat()
        try:
            for user_id in connection_manager.connected_user_ids():
                await connection_manager.send_to_user(
                    user_id,
                    {
                        "type": "proactive_message",
                        "message": "Я на связи. Хотите, помогу с задачами на сегодня?",
                        "timestamp": now,
                    },
                )
            success = True
        except Exception as exc:
            alerting_service.emit(
                component="scheduler",
                severity="warning",
                message="scheduler proactive ping failed",
                details={"error": str(exc)},
            )
            raise
        finally:
            observability_metrics_service.record(
                component="scheduler",
                operation="proactive_ping",
                success=success,
                latency_ms=(perf_counter() - started_at) * 1000,
            )

    def remove_jobs_for_user(self, user_id: str) -> int:
        removed = 0
        for job in self.scheduler.get_jobs():
            kwargs = getattr(job, "kwargs", None) or {}
            if str(kwargs.get("user_id", "")) != str(user_id):
                continue
            self.scheduler.remove_job(job.id)
            removed += 1
        return removed


scheduler_service = SchedulerService()
