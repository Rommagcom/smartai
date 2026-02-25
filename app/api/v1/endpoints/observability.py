from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

from app.api.types import CurrentUser
from app.services.alerting_service import alerting_service
from app.services.observability_metrics_service import observability_metrics_service

router = APIRouter()


def _require_admin(current_user: CurrentUser) -> None:
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")


@router.get("/metrics")
async def observability_metrics(current_user: CurrentUser) -> dict:
    _require_admin(current_user)
    return observability_metrics_service.snapshot()


@router.get("/metrics/prometheus", response_class=PlainTextResponse)
async def observability_metrics_prometheus(current_user: CurrentUser) -> str:
    _require_admin(current_user)
    return observability_metrics_service.to_prometheus()


@router.get("/alerts")
async def observability_alerts(
    current_user: CurrentUser,
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    _require_admin(current_user)
    return {"items": alerting_service.list_alerts(limit=limit)}
