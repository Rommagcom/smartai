from contextlib import asynccontextmanager, suppress
import asyncio
import logging

from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.logging import setup_logging
from app.services.alerting_service import alerting_service
from app.services.http_client_service import http_client_service
from app.services.milvus_service import milvus_service
from app.services.scheduler_service import scheduler_service
from app.services.websocket_manager import connection_manager
from app.workers.worker_service import worker_service

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker_task: asyncio.Task | None = None
    try:
        connection_manager.start()
        milvus_service.ensure_collection()
    except Exception as exc:
        logger.warning("Milvus init skipped", extra={"context": {"error": str(exc)}})
        alerting_service.emit(component="startup", severity="warning", message="Milvus init skipped", details={"error": str(exc)})

    if settings.SCHEDULER_ENABLED:
        scheduler_service.start()
        try:
            await scheduler_service.bootstrap_from_db()
        except Exception as exc:
            logger.exception("scheduler bootstrap failed")
            alerting_service.emit(component="startup", severity="warning", message="Scheduler bootstrap failed", details={"error": str(exc)})
    else:
        logger.info("scheduler disabled", extra={"context": {"component": "scheduler", "event": "disabled"}})

    if settings.WORKER_ENABLED:
        worker_task = asyncio.create_task(worker_service.run_forever())
    else:
        logger.info("worker disabled", extra={"context": {"component": "worker", "event": "disabled"}})

    logger.info("application started", extra={"context": {"component": "app", "event": "startup"}})
    yield
    if settings.SCHEDULER_ENABLED:
        scheduler_service.shutdown()
    if worker_task:
        worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await worker_task
    try:
        await connection_manager.stop()
    except Exception as exc:
        logger.warning("websocket manager stop failed", extra={"context": {"error": str(exc)}})
        alerting_service.emit(
            component="startup",
            severity="warning",
            message="Websocket manager stop failed",
            details={"error": str(exc)},
        )
    try:
        await http_client_service.close()
    except Exception as exc:
        logger.warning("http client close failed", extra={"context": {"error": str(exc)}})
        alerting_service.emit(
            component="startup",
            severity="warning",
            message="HTTP client close failed",
            details={"error": str(exc)},
        )
    logger.info("application stopped", extra={"context": {"component": "app", "event": "shutdown"}})


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
