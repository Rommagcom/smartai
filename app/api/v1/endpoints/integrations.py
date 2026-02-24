from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.api.types import CurrentUser, DBSession
from app.models.api_integration import ApiIntegration
from app.schemas.integration import (
    IntegrationAuthDataRotateResponse,
    IntegrationCreate,
    IntegrationHealthResponse,
    IntegrationOnboardingConnectRequest,
    IntegrationOnboardingConnectResponse,
    IntegrationOnboardingSaveRequest,
    IntegrationOnboardingSaveResponse,
    IntegrationOnboardingStatusResponse,
    IntegrationOnboardingTestRequest,
    IntegrationOnboardingTestResponse,
    IntegrationOut,
)
from app.services.api_executor import api_executor
from app.services.auth_data_security_service import auth_data_security_service
from app.services.integration_onboarding_service import integration_onboarding_service

router = APIRouter()


def _require_admin(current_user: CurrentUser) -> None:
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")


async def _resolve_integration_auth_data(*, db: DBSession, integration: ApiIntegration) -> dict:
    try:
        auth_data, rotated = auth_data_security_service.resolve_for_runtime(integration.auth_data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to decrypt integration auth_data: {exc}") from exc
    if rotated is not None:
        integration.auth_data = rotated
        db.add(integration)
        await db.commit()
        await db.refresh(integration)
    return auth_data


def _resolve_onboarding_draft_payload(*, current_user: CurrentUser, draft_id: str, draft: object) -> tuple[dict | None, dict | None]:
    state = integration_onboarding_service.get_session(user_id=str(current_user.id), draft_id=draft_id) if draft_id else None
    if draft is not None:
        draft_payload = draft.model_dump()
        return draft_payload, state
    if state and isinstance(state.get("draft"), dict):
        return state.get("draft"), state
    return None, state


def _normalize_onboarding_draft(draft_payload: dict) -> dict:
    return integration_onboarding_service.build_draft(
        service_name=str(draft_payload.get("service_name") or "custom-api"),
        token=str((draft_payload.get("auth_data") or {}).get("token") or ""),
        base_url=str((draft_payload.get("auth_data") or {}).get("base_url") or ""),
        endpoints=draft_payload.get("endpoints") if isinstance(draft_payload.get("endpoints"), list) else [],
        healthcheck=draft_payload.get("healthcheck") if isinstance(draft_payload.get("healthcheck"), dict) else None,
    )


def _ensure_onboarding_session(*, current_user: CurrentUser, draft_id: str, state: dict | None, draft: dict) -> str:
    if state and draft_id:
        return draft_id
    created = integration_onboarding_service.create_session(user_id=str(current_user.id), draft=draft)
    return str(created.get("draft_id") or "")


@router.post("", response_model=IntegrationOut)
async def create_integration(
    payload: IntegrationCreate,
    db: DBSession,
    current_user: CurrentUser,
) -> ApiIntegration:
    integration = ApiIntegration(
        user_id=current_user.id,
        service_name=payload.service_name,
        auth_data=auth_data_security_service.encrypt(payload.auth_data),
        endpoints=payload.endpoints,
        is_active=payload.is_active,
    )
    db.add(integration)
    await db.commit()
    await db.refresh(integration)
    return integration


@router.post("/onboarding/connect", response_model=IntegrationOnboardingConnectResponse)
async def onboarding_connect(
    payload: IntegrationOnboardingConnectRequest,
    current_user: CurrentUser,
) -> IntegrationOnboardingConnectResponse:
    draft = integration_onboarding_service.build_draft(
        service_name=payload.service_name,
        token=payload.token,
        base_url=payload.base_url,
        endpoints=[item.model_dump() for item in payload.endpoints],
        healthcheck=payload.healthcheck.model_dump() if payload.healthcheck else None,
    )
    state = integration_onboarding_service.create_session(user_id=str(current_user.id), draft=draft)
    return IntegrationOnboardingConnectResponse(
        draft_id=str(state.get("draft_id") or ""),
        step=str(state.get("step") or "connected"),
        draft=draft,
        message="Подключение подготовлено. Следующий шаг: onboarding/test.",
    )


@router.post("/onboarding/test", response_model=IntegrationOnboardingTestResponse)
async def onboarding_test(
    payload: IntegrationOnboardingTestRequest,
    current_user: CurrentUser,
) -> IntegrationOnboardingTestResponse:
    draft_id = str(payload.draft_id or "").strip()
    draft_payload, state = _resolve_onboarding_draft_payload(current_user=current_user, draft_id=draft_id, draft=payload.draft)
    if not isinstance(draft_payload, dict):
        raise HTTPException(status_code=400, detail="Onboarding draft is required")

    draft = _normalize_onboarding_draft(draft_payload)
    test_result = await integration_onboarding_service.test_draft(draft)
    draft_id = _ensure_onboarding_session(current_user=current_user, draft_id=draft_id, state=state, draft=draft)
    integration_onboarding_service.update_after_test(
        user_id=str(current_user.id),
        draft_id=draft_id,
        draft=draft,
        test=test_result,
    )
    return IntegrationOnboardingTestResponse(draft_id=draft_id, step="tested", draft=draft, test=test_result)


@router.post("/onboarding/save", response_model=IntegrationOnboardingSaveResponse)
async def onboarding_save(
    payload: IntegrationOnboardingSaveRequest,
    db: DBSession,
    current_user: CurrentUser,
) -> IntegrationOnboardingSaveResponse:
    draft_id = str(payload.draft_id or "").strip()
    draft_payload, state = _resolve_onboarding_draft_payload(current_user=current_user, draft_id=draft_id, draft=payload.draft)
    if not isinstance(draft_payload, dict):
        raise HTTPException(status_code=400, detail="Onboarding draft is required")

    draft = _normalize_onboarding_draft(draft_payload)
    test_result = await integration_onboarding_service.test_draft(draft)
    if payload.require_successful_test and not bool(test_result.get("success")):
        raise HTTPException(status_code=400, detail={"message": "Integration healthcheck failed", "test": test_result})

    integration = await integration_onboarding_service.save_draft(
        db=db,
        user_id=current_user.id,
        draft=draft,
        is_active=payload.is_active,
    )
    draft_id = _ensure_onboarding_session(current_user=current_user, draft_id=draft_id, state=state, draft=draft)
    integration_onboarding_service.update_after_save(
        user_id=str(current_user.id),
        draft_id=draft_id,
        integration_id=str(integration.id),
    )
    return IntegrationOnboardingSaveResponse(draft_id=draft_id, step="saved", integration=integration, test=test_result)


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


@router.get("/onboarding/status/{draft_id}", response_model=IntegrationOnboardingStatusResponse)
async def onboarding_status(
    draft_id: str,
    current_user: CurrentUser,
) -> IntegrationOnboardingStatusResponse:
    state = integration_onboarding_service.get_session(user_id=str(current_user.id), draft_id=draft_id)
    if not state:
        raise HTTPException(status_code=404, detail="Onboarding draft not found")
    return IntegrationOnboardingStatusResponse(**integration_onboarding_service.build_status_response(state))


@router.get("/{integration_id}/health", response_model=IntegrationHealthResponse)
async def integration_health(
    integration_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
) -> IntegrationHealthResponse:
    result = await db.execute(
        select(ApiIntegration).where(ApiIntegration.id == integration_id, ApiIntegration.user_id == current_user.id)
    )
    integration = result.scalar_one_or_none()
    if not integration:
        raise HTTPException(status_code=404, detail="Integration not found")

    await _resolve_integration_auth_data(db=db, integration=integration)
    health_result = await integration_onboarding_service.check_health(integration)
    return IntegrationHealthResponse(**health_result)


@router.post("/admin/rotate-auth-data", response_model=IntegrationAuthDataRotateResponse)
async def admin_rotate_auth_data(
    db: DBSession,
    current_user: CurrentUser,
) -> IntegrationAuthDataRotateResponse:
    _require_admin(current_user)

    result = await db.execute(select(ApiIntegration).order_by(ApiIntegration.created_at.desc()))
    rows = result.scalars().all()

    scanned = 0
    rotated = 0
    failed = 0
    for integration in rows:
        scanned += 1
        try:
            _, rotated_payload = auth_data_security_service.resolve_for_runtime(integration.auth_data)
            if rotated_payload is not None:
                integration.auth_data = rotated_payload
                db.add(integration)
                rotated += 1
        except Exception:
            failed += 1

    await db.commit()
    return IntegrationAuthDataRotateResponse(scanned=scanned, rotated=rotated, failed=failed)


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
    auth_data = await _resolve_integration_auth_data(db=db, integration=integration)
    if token := auth_data.get("token"):
        headers["Authorization"] = f"Bearer {token}"

    return await api_executor.call(method=method, url=endpoint, headers=headers, body=payload)
