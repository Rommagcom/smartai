from contextlib import asynccontextmanager, suppress
import asyncio
import logging

from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.logging import setup_logging
from app.services.alerting_service import alerting_service
from app.services.milvus_service import milvus_service
from app.services.scheduler_service import scheduler_service
from app.workers.worker_service import worker_service

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker_task: asyncio.Task | None = None
    try:
        milvus_service.ensure_collection()
    except Exception as exc:
        logger.warning("Milvus init skipped", extra={"context": {"error": str(exc)}})
        alerting_service.emit(component="startup", severity="warning", message="Milvus init skipped", details={"error": str(exc)})
    scheduler_service.start()
    worker_task = asyncio.create_task(worker_service.run_forever())
    logger.info("application started", extra={"context": {"component": "app", "event": "startup"}})
    yield
    scheduler_service.shutdown()
    if worker_task:
        worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await worker_task
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
