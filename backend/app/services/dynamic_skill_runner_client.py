from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.core.config import settings
from app.services.http_client_service import http_client_service


class DynamicSkillRunnerClient:
    async def execute_skill(
        self,
        *,
        skill_code: str,
        function_name: str,
        params: dict[str, Any],
        context: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        client = http_client_service.get()
        url = f"{str(settings.DYNAMIC_SKILL_RUNNER_URL).rstrip('/')}/execute"
        headers = {
            "X-Runner-Secret": str(settings.DYNAMIC_SKILL_RUNNER_SECRET or ""),
        }
        payload = {
            "skill_code": str(skill_code or ""),
            "function_name": str(function_name or "run"),
            "params": dict(params or {}),
            "context": dict(context or {}),
            "timeout_seconds": int(timeout_seconds),
        }

        try:
            async with asyncio.timeout(max(1, int(settings.DYNAMIC_SKILL_RUNNER_TIMEOUT_SECONDS))):
                response = await client.post(url, json=payload, headers=headers)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("skill-runner request timeout") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"skill-runner request failed: {exc}") from exc

        if response.status_code >= 400:
            raise RuntimeError(f"skill-runner returned {response.status_code}: {response.text[:500]}")

        body = response.json() if response.content else {}
        if not isinstance(body, dict):
            raise RuntimeError("skill-runner returned invalid payload")
        return body


dynamic_skill_runner_client = DynamicSkillRunnerClient()
