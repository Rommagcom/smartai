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
        runner_timeout_seconds = max(
            1,
            int(settings.DYNAMIC_SKILL_RUNNER_TIMEOUT_SECONDS),
            int(timeout_seconds) + 10,
        )
        request_timeout = httpx.Timeout(
            timeout=runner_timeout_seconds,
            connect=min(10.0, float(runner_timeout_seconds)),
            read=float(runner_timeout_seconds),
            write=float(runner_timeout_seconds),
            pool=5.0,
        )

        try:
            async with asyncio.timeout(runner_timeout_seconds):
                response = await client.post(url, json=payload, headers=headers, timeout=request_timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"skill-runner request timeout after {runner_timeout_seconds}s") from exc
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"skill-runner request timeout after {runner_timeout_seconds}s") from exc
        except httpx.HTTPError as exc:
            message = str(exc).strip() or exc.__class__.__name__
            raise RuntimeError(f"skill-runner request failed: {message}") from exc

        if response.status_code >= 400:
            raise RuntimeError(f"skill-runner returned {response.status_code}: {response.text[:500]}")

        body = response.json() if response.content else {}
        if not isinstance(body, dict):
            raise RuntimeError("skill-runner returned invalid payload")
        return body


dynamic_skill_runner_client = DynamicSkillRunnerClient()
