from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse

from app.api.types import AdminUser
from app.services.alerting_service import alerting_service
from app.services.observability_metrics_service import observability_metrics_service

router = APIRouter()


@router.get("/metrics")
async def observability_metrics(current_user: AdminUser) -> dict:
    return observability_metrics_service.snapshot()


@router.get("/metrics/prometheus", response_class=PlainTextResponse)
async def observability_metrics_prometheus(current_user: AdminUser) -> str:
    return observability_metrics_service.to_prometheus()


@router.get("/alerts")
async def observability_alerts(
    current_user: AdminUser,
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    return {"items": alerting_service.list_alerts(limit=limit)}
