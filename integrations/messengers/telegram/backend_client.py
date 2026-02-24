from __future__ import annotations

from typing import Any

import httpx

from integrations.messengers.common.auth_bridge import build_backend_credentials


class BackendApiClient:
    def __init__(self, base_url: str, bridge_secret: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.bridge_secret = bridge_secret
        self._session_ids: dict[int, str] = {}

    async def _request(
        self,
        method: str,
        path: str,
        token: str | None = None,
        json: dict | None = None,
        params: dict | None = None,
        files: dict | None = None,
        extra_headers: dict | None = None,
    ) -> dict[str, Any]:
        headers = dict(extra_headers or {})
        if token:
            headers["Authorization"] = f"Bearer {token}"

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.request(
                method=method,
                url=f"{self.base_url}{path}",
                json=json,
                params=params,
                headers=headers,
                files=files,
            )
        payload: Any
        try:
            payload = response.json()
        except Exception:
            payload = {"raw": response.text}
        return {"status": response.status_code, "payload": payload}

    async def is_telegram_allowed(self, telegram_user_id: int) -> bool:
        result = await self._request(
            "GET",
            f"/telegram/access/check/{telegram_user_id}",
            extra_headers={"X-Telegram-Bridge-Secret": self.bridge_secret},
        )
        if result["status"] != 200:
            return False
        return bool(result["payload"].get("allowed", False))

    async def ensure_auth(self, telegram_user_id: int) -> tuple[str, str]:
        username, password = build_backend_credentials(telegram_user_id, self.bridge_secret)
        login_res = await self._request("POST", "/auth/login", json={"username": username, "password": password})
        if login_res["status"] == 200:
            return login_res["payload"]["access_token"], username

        register_res = await self._request("POST", "/auth/register", json={"username": username, "password": password})
        if register_res["status"] != 200:
            raise RuntimeError(f"Auth failed: {register_res['payload']}")

        return register_res["payload"]["access_token"], username

    async def get_me(self, token: str) -> dict[str, Any]:
        return await self._request("GET", "/users/me", token=token)

    async def get_onboarding_next_step(self, token: str) -> dict[str, Any]:
        return await self._request("GET", "/users/me/onboarding-next-step", token=token)

    async def soul_status(self, token: str) -> dict[str, Any]:
        return await self._request("GET", "/users/me/soul/status", token=token)

    async def soul_setup(self, token: str, body: dict) -> dict[str, Any]:
        return await self._request("POST", "/users/me/soul/setup", token=token, json=body)

    async def soul_adapt_task(self, token: str, body: dict) -> dict[str, Any]:
        return await self._request("POST", "/users/me/soul/adapt-task", token=token, json=body)

    async def chat(self, token: str, telegram_user_id: int, message: str) -> dict[str, Any]:
        body: dict[str, Any] = {"message": message}
        session_id = self._session_ids.get(telegram_user_id)
        if session_id:
            body["session_id"] = session_id
        response = await self._request("POST", "/chat", token=token, json=body)
        if response["status"] == 200 and response["payload"].get("session_id"):
            self._session_ids[telegram_user_id] = response["payload"]["session_id"]
        return response

    async def chat_history(self, token: str, session_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/chat/history/{session_id}", token=token)

    async def chat_self_improve(self, token: str) -> dict[str, Any]:
        return await self._request("POST", "/chat/self-improve", token=token)

    async def execute_python(self, token: str, code: str) -> dict[str, Any]:
        return await self._request("POST", "/chat/execute-python", token=token, json={"code": code})

    async def worker_results_poll(self, token: str, limit: int = 20) -> dict[str, Any]:
        return await self._request("GET", "/chat/worker-results/poll", token=token, params={"limit": limit})

    async def web_search(self, token: str, query: str, limit: int = 5) -> dict[str, Any]:
        return await self._request("POST", "/chat/tools/web-search", token=token, json={"query": query, "limit": limit})

    async def web_fetch(self, token: str, url: str, max_chars: int = 12000) -> dict[str, Any]:
        return await self._request("POST", "/chat/tools/web-fetch", token=token, json={"url": url, "max_chars": max_chars})

    async def browser_action(self, token: str, url: str, action: str = "extract_text") -> dict[str, Any]:
        return await self._request("POST", "/chat/tools/browser", token=token, json={"url": url, "action": action})

    async def pdf_create(self, token: str, title: str, content: str, filename: str = "document.pdf") -> dict[str, Any]:
        return await self._request(
            "POST",
            "/chat/tools/pdf-create",
            token=token,
            json={"title": title, "content": content, "filename": filename},
        )

    async def memory_add(self, token: str, fact_type: str, content: str, importance_score: float) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/memory",
            token=token,
            json={"fact_type": fact_type, "content": content, "importance_score": importance_score},
        )

    async def memory_list(self, token: str) -> dict[str, Any]:
        return await self._request("GET", "/memory", token=token)

    async def documents_upload(self, token: str, filename: str, content: bytes) -> dict[str, Any]:
        files = {"file": (filename, content)}
        return await self._request("POST", "/documents/upload", token=token, files=files)

    async def documents_search(self, token: str, query: str, top_k: int = 5) -> dict[str, Any]:
        return await self._request("GET", "/documents/search", token=token, params={"query": query, "top_k": top_k})

    async def cron_add(self, token: str, body: dict) -> dict[str, Any]:
        return await self._request("POST", "/cron", token=token, json=body)

    async def cron_list(self, token: str) -> dict[str, Any]:
        return await self._request("GET", "/cron", token=token)

    async def cron_delete(self, token: str, job_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/cron/{job_id}", token=token)

    async def integrations_add(self, token: str, body: dict) -> dict[str, Any]:
        return await self._request("POST", "/integrations", token=token, json=body)

    async def integrations_list(self, token: str) -> dict[str, Any]:
        return await self._request("GET", "/integrations", token=token)

    async def integrations_call(self, token: str, integration_id: str, body: dict) -> dict[str, Any]:
        return await self._request("POST", f"/integrations/{integration_id}/call", token=token, json=body)
