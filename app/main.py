from contextlib import asynccontextmanager, suppress
import asyncio

from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.config import settings
from app.services.milvus_service import milvus_service
from app.services.scheduler_service import scheduler_service
from app.workers.worker_service import worker_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker_task: asyncio.Task | None = None
    try:
        milvus_service.ensure_collection()
    except Exception as exc:
        print(f"Milvus init skipped: {exc}")
    scheduler_service.start()
    worker_task = asyncio.create_task(worker_service.run_forever())
    yield
    scheduler_service.shutdown()
    if worker_task:
        worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await worker_task


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
