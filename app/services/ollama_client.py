from typing import AsyncGenerator

import httpx

from app.core.config import settings


class OllamaClient:
    async def chat(self, messages: list[dict], stream: bool = False, options: dict | None = None) -> str:
        payload = {
            "model": settings.OLLAMA_MODEL_NAME,
            "messages": messages,
            "stream": stream,
            "options": options or {},
        }
        async with httpx.AsyncClient(timeout=settings.OLLAMA_TIMEOUT_SECONDS) as client:
            response = await client.post(f"{settings.OLLAMA_BASE_URL}/api/chat", json=payload)
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
        async with httpx.AsyncClient(timeout=settings.OLLAMA_TIMEOUT_SECONDS) as client:
            async with client.stream("POST", f"{settings.OLLAMA_BASE_URL}/api/chat", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    yield line

    async def embeddings(self, text: str) -> list[float]:
        payload = {
            "model": "nomic-embed-text",
            "prompt": text,
        }
        async with httpx.AsyncClient(timeout=settings.OLLAMA_TIMEOUT_SECONDS) as client:
            response = await client.post(f"{settings.OLLAMA_BASE_URL}/api/embeddings", json=payload)
            response.raise_for_status()
            return response.json().get("embedding", [])


ollama_client = OllamaClient()
