from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class IntegrationCreate(BaseModel):
    service_name: str
    auth_data: dict
    endpoints: list[dict] = []
    is_active: bool = True


class IntegrationOut(BaseModel):
    id: UUID
    service_name: str
    endpoints: list
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class IntegrationEndpointSpec(BaseModel):
    name: str | None = None
    url: str
    method: str = "GET"
    headers: dict = {}
    payload: dict | None = None


class IntegrationHealthcheckSpec(BaseModel):
    url: str | None = None
    method: str = "GET"
    headers: dict = {}
    payload: dict | None = None


class IntegrationOnboardingConnectRequest(BaseModel):
    service_name: str
    token: str | None = None
    base_url: str | None = None
    endpoints: list[IntegrationEndpointSpec] = []
    healthcheck: IntegrationHealthcheckSpec | None = None


class IntegrationOnboardingDraft(BaseModel):
    service_name: str
    auth_data: dict = {}
    endpoints: list[dict] = []
    healthcheck: dict = {}


class IntegrationOnboardingConnectResponse(BaseModel):
    draft_id: str
    step: str
    draft: IntegrationOnboardingDraft
    message: str


class IntegrationOnboardingTestRequest(BaseModel):
    draft_id: str | None = None
    draft: IntegrationOnboardingDraft | None = None


class IntegrationOnboardingTestResult(BaseModel):
    success: bool
    status_code: int | None = None
    message: str
    response_preview: str | None = None


class IntegrationOnboardingTestResponse(BaseModel):
    draft_id: str
    step: str
    draft: IntegrationOnboardingDraft
    test: IntegrationOnboardingTestResult


class IntegrationOnboardingSaveRequest(BaseModel):
    draft_id: str | None = None
    draft: IntegrationOnboardingDraft | None = None
    is_active: bool = True
    require_successful_test: bool = False


class IntegrationOnboardingSaveResponse(BaseModel):
    draft_id: str
    step: str
    integration: IntegrationOut
    test: IntegrationOnboardingTestResult | None = None


class IntegrationOnboardingStatusResponse(BaseModel):
    draft_id: str
    step: str
    draft: IntegrationOnboardingDraft
    last_test: IntegrationOnboardingTestResult | None = None
    saved_integration_id: UUID | None = None
    updated_at: datetime


class IntegrationHealthResponse(BaseModel):
    integration_id: UUID
    service_name: str
    is_active: bool
    healthcheck: dict
    health: IntegrationOnboardingTestResult


class IntegrationAuthDataRotateResponse(BaseModel):
    scanned: int
    rotated: int
    failed: int
