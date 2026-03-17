from __future__ import annotations

import asyncio
import json
import logging
from time import perf_counter
from typing import Any

from app.services.http_client_service import http_client_service
from integrations.messengers.common.auth_bridge import build_backend_credentials_for_subject
from integrations.messengers.teams.settings import get_teams_settings

logger = logging.getLogger(__name__)


def _safe_payload(payload: Any, max_len: int = 1200) -> str:
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except Exception:
        text = str(payload)
    return text if len(text) <= max_len else f"{text[:max_len]}..."


class TeamsBackendApiClient:
    def __init__(self, base_url: str, bridge_secret: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.bridge_secret = bridge_secret
        self._verbose_logging = bool(get_teams_settings().DEV_VERBOSE_LOGGING)

    async def _request(
        self,
        method: str,
        path: str,
        token: str | None = None,
        json: dict | None = None,
        params: dict | None = None,
    ) -> dict[str, Any]:
        started_at = perf_counter()
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        if self._verbose_logging:
            logger.info(
                "teams bridge request",
                extra={
                    "context": {
                        "component": "teams_bridge",
                        "event": "api_request",
                        "method": method.upper(),
                        "path": path,
                        "has_json": bool(json),
                        "has_params": bool(params),
                    }
                },
            )

        client = http_client_service.get()
        async with asyncio.timeout(get_teams_settings().TEAMS_DEFAULT_TIMEOUT_SECONDS):
            response = await client.request(
                method=method,
                url=f"{self.base_url}{path}",
                json=json,
                params=params,
                headers=headers,
            )

        payload: Any
        try:
            payload = response.json()
        except Exception:
            payload = {"raw": response.text}

        if self._verbose_logging:
            logger.info(
                "teams bridge response",
                extra={
                    "context": {
                        "component": "teams_bridge",
                        "event": "api_response",
                        "method": method.upper(),
                        "path": path,
                        "status": response.status_code,
                        "latency_ms": round((perf_counter() - started_at) * 1000, 2),
                        "payload": _safe_payload(payload),
                    }
                },
            )
        return {"status": response.status_code, "payload": payload}

    async def ensure_auth(self, subject_id: str) -> tuple[str, str]:
        username, password = build_backend_credentials_for_subject(
            subject_prefix="teams",
            subject_id=subject_id,
            secret=self.bridge_secret,
        )
        login_res = await self._request("POST", "/auth/login", json={"username": username, "password": password})
        if login_res["status"] == 200:
            return login_res["payload"]["access_token"], username

        register_res = await self._request("POST", "/auth/register", json={"username": username, "password": password})
        if register_res["status"] != 200:
            raise RuntimeError(f"Auth failed: {register_res['payload']}")
        return register_res["payload"]["access_token"], username

    async def chat(self, token: str, message: str, session_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"message": message}
        if session_id:
            body["session_id"] = session_id
        async with asyncio.timeout(get_teams_settings().TEAMS_CHAT_TIMEOUT_SECONDS):
            return await self._request("POST", "/chat", token=token, json=body)
