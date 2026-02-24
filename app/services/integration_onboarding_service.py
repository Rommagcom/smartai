from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.api_integration import ApiIntegration
from app.services.api_executor import api_executor
from app.services.auth_data_security_service import auth_data_security_service


class IntegrationOnboardingService:
    def __init__(self) -> None:
        self._sessions: dict[str, dict] = {}

    @staticmethod
    def build_draft(
        *,
        service_name: str,
        token: str | None = None,
        base_url: str | None = None,
        endpoints: list[dict] | None = None,
        healthcheck: dict | None = None,
    ) -> dict:
        normalized_endpoints = [item for item in (endpoints or []) if isinstance(item, dict)]
        normalized_auth_data: dict[str, Any] = {}

        token_value = str(token or "").strip()
        base_url_value = str(base_url or "").strip()
        if token_value:
            normalized_auth_data["token"] = token_value
        if base_url_value:
            normalized_auth_data["base_url"] = base_url_value.rstrip("/")

        normalized_healthcheck = IntegrationOnboardingService._normalize_healthcheck(
            healthcheck=healthcheck,
            endpoints=normalized_endpoints,
            base_url=normalized_auth_data.get("base_url", ""),
        )

        return {
            "service_name": str(service_name or "custom-api").strip() or "custom-api",
            "auth_data": normalized_auth_data,
            "endpoints": normalized_endpoints,
            "healthcheck": normalized_healthcheck,
        }

    @staticmethod
    async def test_draft(draft: dict) -> dict:
        healthcheck = draft.get("healthcheck") if isinstance(draft.get("healthcheck"), dict) else {}
        auth_data = draft.get("auth_data") if isinstance(draft.get("auth_data"), dict) else {}

        endpoint = str(healthcheck.get("url") or "").strip()
        method = str(healthcheck.get("method") or "GET").strip().upper() or "GET"
        payload = healthcheck.get("payload") if isinstance(healthcheck.get("payload"), dict) else None
        headers = dict(healthcheck.get("headers") or {}) if isinstance(healthcheck.get("headers"), dict) else {}

        if not endpoint:
            return {
                "success": False,
                "status_code": None,
                "message": "Healthcheck url is required",
                "response_preview": None,
            }

        token = str(auth_data.get("token") or "").strip()
        if token:
            headers.setdefault("Authorization", f"Bearer {token}")

        try:
            response = await api_executor.call(method=method, url=endpoint, headers=headers, body=payload)
            status_code = int(response.get("status_code") or 0)
            body = str(response.get("body") or "")
            return {
                "success": 200 <= status_code < 400,
                "status_code": status_code,
                "message": "Healthcheck passed" if 200 <= status_code < 400 else "Healthcheck failed",
                "response_preview": body[:1000],
            }
        except Exception as exc:
            return {
                "success": False,
                "status_code": None,
                "message": f"Healthcheck error: {exc}",
                "response_preview": None,
            }

    @staticmethod
    async def save_draft(
        *,
        db: AsyncSession,
        user_id: Any,
        draft: dict,
        is_active: bool = True,
    ) -> ApiIntegration:
        integration = ApiIntegration(
            user_id=user_id,
            service_name=str(draft.get("service_name") or "custom-api"),
            auth_data=auth_data_security_service.encrypt(
                draft.get("auth_data") if isinstance(draft.get("auth_data"), dict) else {}
            ),
            endpoints=draft.get("endpoints") if isinstance(draft.get("endpoints"), list) else [],
            is_active=bool(is_active),
        )
        db.add(integration)
        await db.commit()
        await db.refresh(integration)
        return integration

    @staticmethod
    async def check_health(integration: ApiIntegration) -> dict:
        try:
            auth_data, _ = auth_data_security_service.resolve_for_runtime(integration.auth_data)
        except Exception as exc:
            return {
                "integration_id": str(integration.id),
                "service_name": integration.service_name,
                "is_active": integration.is_active,
                "healthcheck": {},
                "health": {
                    "success": False,
                    "status_code": None,
                    "message": f"Auth data decrypt error: {exc}",
                    "response_preview": None,
                },
            }
        endpoints = integration.endpoints if isinstance(integration.endpoints, list) else []

        healthcheck = IntegrationOnboardingService._normalize_healthcheck(
            healthcheck=None,
            endpoints=endpoints,
            base_url=str(auth_data.get("base_url") or ""),
        )
        draft = {
            "service_name": integration.service_name,
            "auth_data": auth_data,
            "endpoints": endpoints,
            "healthcheck": healthcheck,
        }
        result = await IntegrationOnboardingService.test_draft(draft)
        return {
            "integration_id": str(integration.id),
            "service_name": integration.service_name,
            "is_active": integration.is_active,
            "healthcheck": healthcheck,
            "health": result,
        }

    def create_session(self, *, user_id: str, draft: dict) -> dict:
        draft_id = uuid4().hex
        state = {
            "draft_id": draft_id,
            "user_id": str(user_id),
            "step": "connected",
            "draft": draft,
            "last_test": None,
            "saved_integration_id": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._sessions[draft_id] = state
        return state

    def get_session(self, *, user_id: str, draft_id: str) -> dict | None:
        state = self._sessions.get(str(draft_id))
        if not state:
            return None
        if str(state.get("user_id") or "") != str(user_id):
            return None
        return state

    def update_after_test(self, *, user_id: str, draft_id: str, draft: dict, test: dict) -> dict | None:
        state = self.get_session(user_id=user_id, draft_id=draft_id)
        if not state:
            return None
        state["draft"] = draft
        state["last_test"] = test
        state["step"] = "tested"
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        return state

    def update_after_save(self, *, user_id: str, draft_id: str, integration_id: str) -> dict | None:
        state = self.get_session(user_id=user_id, draft_id=draft_id)
        if not state:
            return None
        state["step"] = "saved"
        state["saved_integration_id"] = integration_id
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        return state

    def build_status_response(self, state: dict) -> dict:
        return {
            "draft_id": str(state.get("draft_id") or ""),
            "step": str(state.get("step") or "connected"),
            "draft": state.get("draft") if isinstance(state.get("draft"), dict) else {},
            "last_test": state.get("last_test") if isinstance(state.get("last_test"), dict) else None,
            "saved_integration_id": state.get("saved_integration_id"),
            "updated_at": str(state.get("updated_at") or datetime.now(timezone.utc).isoformat()),
        }

    @staticmethod
    def _normalize_healthcheck(*, healthcheck: dict | None, endpoints: list[dict], base_url: str) -> dict:
        candidate = dict(healthcheck or {}) if isinstance(healthcheck, dict) else {}

        health_url = str(candidate.get("url") or "").strip()
        if not health_url:
            health_url = IntegrationOnboardingService._default_health_url(endpoints=endpoints, base_url=base_url)

        method = str(candidate.get("method") or "GET").strip().upper() or "GET"
        headers = dict(candidate.get("headers") or {}) if isinstance(candidate.get("headers"), dict) else {}
        payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else None

        normalized: dict[str, Any] = {
            "url": health_url,
            "method": method,
            "headers": headers,
        }
        if payload is not None:
            normalized["payload"] = payload
        return normalized

    @staticmethod
    def _default_health_url(*, endpoints: list[dict], base_url: str) -> str:
        for endpoint in endpoints:
            url = str(endpoint.get("url") or "").strip() if isinstance(endpoint, dict) else ""
            if url:
                return url

        base_url_value = str(base_url or "").strip().rstrip("/")
        if not base_url_value:
            return ""
        return urljoin(f"{base_url_value}/", "health")


integration_onboarding_service = IntegrationOnboardingService()
