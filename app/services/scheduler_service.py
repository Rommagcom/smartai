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
from app.services.observability_metrics_service import observability_metrics_service
from app.services.websocket_manager import connection_manager

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
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(CronJob).where(CronJob.is_active.is_(True)).order_by(CronJob.created_at.desc())
                )
                rows = result.scalars().all()

            for row in rows:
                try:
                    self.add_or_replace_job(
                        job_id=str(row.id),
                        cron_expression=row.cron_expression,
                        user_id=str(row.user_id),
                        action_type=row.action_type,
                        payload=row.payload if isinstance(row.payload, dict) else {},
                    )
                    loaded += 1
                except Exception:
                    failed += 1

            success = failed == 0
            logger.info(
                "scheduler bootstrap complete",
                extra={
                    "context": {
                        "component": "scheduler",
                        "event": "bootstrap",
                        "loaded": loaded,
                        "failed": failed,
                    }
                },
            )
            return {"loaded": loaded, "failed": failed}
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
            if action_type == "send_message":
                await connection_manager.send_to_user(
                    user_id,
                    {
                        "type": "proactive_message",
                        "message": payload.get("message", "Напоминание от ассистента"),
                        "timestamp": now,
                    },
                )
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


scheduler_service = SchedulerService()
