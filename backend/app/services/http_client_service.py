from __future__ import annotations

import httpx

from app.core.config import settings


class HttpClientService:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def get(self) -> httpx.AsyncClient:
        if self._client is None:
            limits = httpx.Limits(
                max_connections=max(10, int(settings.HTTP_CLIENT_MAX_CONNECTIONS)),
                max_keepalive_connections=max(5, int(settings.HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS)),
                keepalive_expiry=max(1.0, float(settings.HTTP_CLIENT_KEEPALIVE_EXPIRY_SECONDS)),
            )
            self._client = httpx.AsyncClient(limits=limits)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


http_client_service = HttpClientService()
