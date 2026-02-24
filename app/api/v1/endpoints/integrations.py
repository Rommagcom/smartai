from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.api.types import CurrentUser, DBSession
from app.models.api_integration import ApiIntegration
from app.models.user import User
from app.schemas.integration import IntegrationCreate, IntegrationOut
from app.services.api_executor import api_executor

router = APIRouter()


@router.post("", response_model=IntegrationOut)
async def create_integration(
    payload: IntegrationCreate,
    db: DBSession,
    current_user: CurrentUser,
) -> ApiIntegration:
    integration = ApiIntegration(
        user_id=current_user.id,
        service_name=payload.service_name,
        auth_data=payload.auth_data,
        endpoints=payload.endpoints,
        is_active=payload.is_active,
    )
    db.add(integration)
    await db.commit()
    await db.refresh(integration)
    return integration


@router.get("", response_model=list[IntegrationOut])
async def list_integrations(
    db: DBSession,
    current_user: CurrentUser,
) -> list[ApiIntegration]:
    result = await db.execute(
        select(ApiIntegration)
        .where(ApiIntegration.user_id == current_user.id)
        .order_by(ApiIntegration.created_at.desc())
    )
    return result.scalars().all()


@router.post("/{integration_id}/call", responses={404: {"description": "Integration not found"}, 400: {"description": "url is required"}})
async def call_integration(
    integration_id: UUID,
    body: dict,
    db: DBSession,
    current_user: CurrentUser,
) -> dict:
    result = await db.execute(
        select(ApiIntegration).where(ApiIntegration.id == integration_id, ApiIntegration.user_id == current_user.id)
    )
    integration = result.scalar_one_or_none()
    if not integration:
        raise HTTPException(status_code=404, detail="Integration not found")

    endpoint = body.get("url")
    method = body.get("method", "GET")
    payload = body.get("payload")
    if not endpoint:
        raise HTTPException(status_code=400, detail="url is required")

    headers = body.get("headers", {})
    if token := integration.auth_data.get("token"):
        headers["Authorization"] = f"Bearer {token}"

    return await api_executor.call(method=method, url=endpoint, headers=headers, body=payload)
