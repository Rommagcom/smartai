import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.services.scheduler_service import scheduler_service
from scripts.smoke_api_flow import run as run_api_flow
from scripts.smoke_admin_access import run as run_admin_access
from scripts.smoke_chat_tools_reminders import run as run_chat_tools_reminders
from scripts.smoke_chat_self_service import run as run_chat_self_service
from scripts.smoke_integrations import run as run_integrations
from scripts.smoke_memory_docs import run as run_memory_docs
from scripts.smoke_onboarding_step import run as run_onboarding_step
from scripts.smoke_telegram_bridge import run as run_telegram_bridge
from scripts.smoke_worker_chat_flow import run as run_worker_chat_flow
from scripts.smoke_worker_queue import run as run_worker_queue
from scripts.smoke_ws_cron import run as run_ws_cron


def reset_scheduler() -> None:
    if scheduler_service.scheduler.running:
        scheduler_service.shutdown()
    scheduler_service.scheduler = AsyncIOScheduler(timezone="UTC")


async def run() -> None:
    print("RUN_SMOKE_API_FLOW")
    await run_api_flow()

    reset_scheduler()

    print("RUN_SMOKE_ADMIN_ACCESS")
    await run_admin_access()

    reset_scheduler()

    print("RUN_SMOKE_WS_CRON")
    await run_ws_cron()

    reset_scheduler()

    print("RUN_SMOKE_MEMORY_DOCS")
    await run_memory_docs()

    reset_scheduler()

    print("RUN_SMOKE_CHAT_TOOLS_REMINDERS")
    await run_chat_tools_reminders()

    reset_scheduler()

    print("RUN_SMOKE_CHAT_SELF_SERVICE")
    await run_chat_self_service()

    reset_scheduler()

    print("RUN_SMOKE_INTEGRATIONS")
    await run_integrations()

    reset_scheduler()

    print("RUN_SMOKE_ONBOARDING_STEP")
    await run_onboarding_step()

    reset_scheduler()

    print("RUN_SMOKE_TELEGRAM_BRIDGE")
    await run_telegram_bridge()

    print("RUN_SMOKE_WORKER_QUEUE")
    await run_worker_queue()

    print("RUN_SMOKE_WORKER_CHAT_FLOW")
    await run_worker_chat_flow()

    print("SMOKE_ALL_OK")


if __name__ == "__main__":
    asyncio.run(run())
