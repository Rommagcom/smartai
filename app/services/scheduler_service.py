from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.services.websocket_manager import connection_manager


class SchedulerService:
    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler(timezone="UTC")

    def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()
            self.scheduler.add_job(self.periodic_proactive_ping, "interval", minutes=30, id="global_proactive_ping", replace_existing=True)

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def add_or_replace_job(self, job_id: str, cron_expression: str, user_id: str, action_type: str, payload: dict) -> None:
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

    async def execute_action(self, user_id: str, action_type: str, payload: dict) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if action_type == "send_message":
            await connection_manager.send_to_user(
                user_id,
                {
                    "type": "proactive_message",
                    "message": payload.get("message", "Напоминание от ассистента"),
                    "timestamp": now,
                },
            )

    async def periodic_proactive_ping(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        for user_id in connection_manager.connected_user_ids():
            await connection_manager.send_to_user(
                user_id,
                {
                    "type": "proactive_message",
                    "message": "Я на связи. Хотите, помогу с задачами на сегодня?",
                    "timestamp": now,
                },
            )


scheduler_service = SchedulerService()
