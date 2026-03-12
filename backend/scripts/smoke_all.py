import asyncio

from scripts.smoke_env import apply_smoke_env_defaults

apply_smoke_env_defaults()

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import settings
from app.services.scheduler_service import scheduler_service
from scripts.smoke_api_flow import run as run_api_flow
from scripts.smoke_admin_access import run as run_admin_access
from scripts.smoke_integrations import run as run_integrations
from scripts.smoke_memory_docs import run as run_memory_docs
from scripts.smoke_chat_cron_add import run as run_chat_cron_add
from scripts.smoke_chat_cron_add_natural import run as run_chat_cron_add_natural
from scripts.smoke_cron_dedupe import run as run_cron_dedupe
from scripts.smoke_onboarding_step import run as run_onboarding_step
from scripts.smoke_telegram_bridge import run as run_telegram_bridge
from scripts.smoke_telegram_admin_delete import run as run_telegram_admin_delete
from scripts.smoke_ws_cron import run as run_ws_cron
from scripts.smoke_tool_routing import run as run_tool_routing
from scripts.smoke_web_compose_fallback import run as run_web_compose_fallback
from scripts.smoke_export_claim_guard import run as run_export_claim_guard
from scripts.smoke_export_claim_guard_e2e import run as run_export_claim_guard_e2e
from scripts.smoke_export_reenqueue_guard import run as run_export_reenqueue_guard
from scripts.smoke_router_clarify_live_export_override import run as run_router_clarify_live_export_override
from scripts.smoke_graph_structured_fallback import run as run_graph_structured_fallback
from scripts.smoke_graph_inline_cron_guard import run as run_graph_inline_cron_guard
from scripts.smoke_document_all_sources import run as run_document_all_sources


def reset_scheduler() -> None:
    if scheduler_service.scheduler.running:
        scheduler_service.shutdown()
    scheduler_service.scheduler = AsyncIOScheduler(timezone="UTC")


async def run() -> None:
    original_scheduler_enabled = settings.SCHEDULER_ENABLED
    original_worker_enabled = settings.WORKER_ENABLED
    original_ws_fanout_enabled = settings.WS_FANOUT_REDIS_ENABLED

    settings.SCHEDULER_ENABLED = False
    settings.WORKER_ENABLED = False
    settings.WS_FANOUT_REDIS_ENABLED = False

    try:
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

        print("RUN_SMOKE_CHAT_CRON_ADD")
        await run_chat_cron_add()

        reset_scheduler()

        print("RUN_SMOKE_CHAT_CRON_ADD_NATURAL")
        await run_chat_cron_add_natural()

        reset_scheduler()

        print("RUN_SMOKE_CRON_DEDUPE")
        await run_cron_dedupe()

        reset_scheduler()

        print("SKIP_SMOKE_CHAT_TOOLS_REMINDERS (web tools removed)")
        print("SKIP_SMOKE_CHAT_SELF_SERVICE (web tools removed)")

        reset_scheduler()

        print("RUN_SMOKE_INTEGRATIONS")
        await run_integrations()

        reset_scheduler()

        print("RUN_SMOKE_ONBOARDING_STEP")
        await run_onboarding_step()

        reset_scheduler()

        print("RUN_SMOKE_TELEGRAM_BRIDGE")
        await run_telegram_bridge()

        reset_scheduler()

        print("RUN_SMOKE_TELEGRAM_ADMIN_DELETE")
        await run_telegram_admin_delete()

        print("SKIP_SMOKE_WORKER_QUEUE (web worker jobs removed)")
        print("SKIP_SMOKE_WORKER_CHAT_FLOW (web worker jobs removed)")

        print("RUN_SMOKE_TOOL_ROUTING")
        await run_tool_routing()

        print("RUN_SMOKE_WEB_COMPOSE_FALLBACK")
        await run_web_compose_fallback()

        print("RUN_SMOKE_EXPORT_CLAIM_GUARD")
        run_export_claim_guard()

        print("RUN_SMOKE_EXPORT_CLAIM_GUARD_E2E")
        await run_export_claim_guard_e2e()

        print("RUN_SMOKE_EXPORT_REENQUEUE_GUARD")
        run_export_reenqueue_guard()

        print("RUN_SMOKE_ROUTER_CLARIFY_LIVE_EXPORT_OVERRIDE")
        await run_router_clarify_live_export_override()

        print("RUN_SMOKE_GRAPH_STRUCTURED_FALLBACK")
        await run_graph_structured_fallback()

        print("RUN_SMOKE_GRAPH_INLINE_CRON_GUARD")
        await run_graph_inline_cron_guard()

        print("RUN_SMOKE_DOCUMENT_ALL_SOURCES")
        await run_document_all_sources()

        print("SMOKE_ALL_OK")
    finally:
        settings.SCHEDULER_ENABLED = original_scheduler_enabled
        settings.WORKER_ENABLED = original_worker_enabled
        settings.WS_FANOUT_REDIS_ENABLED = original_ws_fanout_enabled


if __name__ == "__main__":
    asyncio.run(run())
