from typing import AsyncGenerator

from httpx import HTTPStatusError

from app.core.config import settings
from app.services.http_client_service import http_client_service


class OllamaClient:
    async def chat(self, messages: list[dict], stream: bool = False, options: dict | None = None) -> str:
        payload = {
            "model": settings.OLLAMA_MODEL_NAME,
            "messages": messages,
            "stream": stream,
            "options": options or {},
        }
        client = http_client_service.get()
        response = await client.post(
            f"{settings.OLLAMA_BASE_URL}/api/chat",
            json=payload,
            timeout=settings.OLLAMA_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("message", {}).get("content", "")

    async def stream_chat(self, messages: list[dict], options: dict | None = None) -> AsyncGenerator[str, None]:
        payload = {
            "model": settings.OLLAMA_MODEL_NAME,
            "messages": messages,
            "stream": True,
            "options": options or {},
        }
        client = http_client_service.get()
        async with client.stream(
            "POST",
            f"{settings.OLLAMA_BASE_URL}/api/chat",
            json=payload,
            timeout=settings.OLLAMA_TIMEOUT_SECONDS,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                yield line

    async def embeddings(self, text: str) -> list[float]:
        client = http_client_service.get()
        try:
            response = await client.post(
                f"{settings.OLLAMA_BASE_URL}/api/embed",
                json={
                    "model": "nomic-embed-text",
                    "input": text,
                },
                timeout=settings.OLLAMA_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json()
            embeddings = data.get("embeddings")
            if isinstance(embeddings, list) and embeddings:
                first = embeddings[0]
                if isinstance(first, list):
                    return first
                if isinstance(first, (int, float)):
                    return embeddings  # type: ignore[return-value]
            return []
        except HTTPStatusError as exc:
            if exc.response is None or exc.response.status_code != 404:
                raise

        response = await client.post(
            f"{settings.OLLAMA_BASE_URL}/api/embeddings",
            json={
                "model": "nomic-embed-text",
                "prompt": text,
            },
            timeout=settings.OLLAMA_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json().get("embedding", [])


ollama_client = OllamaClient()
