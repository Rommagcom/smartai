from typing import AsyncGenerator

from ollama import AsyncClient  # type: ignore[import-not-found]

from app.core.config import settings


class OllamaClient:
    def __init__(self) -> None:
        self._client = AsyncClient(host=settings.OLLAMA_BASE_URL)

    @staticmethod
    def _field(obj: object, name: str) -> object | None:
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    def _extract_message_content(self, response: object) -> str:
        message = self._field(response, "message")
        if message is None:
            return ""
        content = self._field(message, "content")
        return str(content or "")

    @staticmethod
    def _normalize_embedding_dim(vector: list[float]) -> list[float]:
        target_dim = int(settings.EMBEDDING_DIM)
        if target_dim <= 0:
            return vector
        current_dim = len(vector)
        if current_dim == target_dim:
            return vector
        if current_dim > target_dim:
            return vector[:target_dim]
        return [*vector, *([0.0] * (target_dim - current_dim))]

    async def chat(self, messages: list[dict], stream: bool = False, options: dict | None = None) -> str:
        response = await self._client.chat(
            model=settings.OLLAMA_MODEL_NAME,
            messages=messages,
            stream=stream,
            options=options or {},
        )
        return self._extract_message_content(response)

    async def stream_chat(self, messages: list[dict], options: dict | None = None) -> AsyncGenerator[str, None]:
        stream = await self._client.chat(
            model=settings.OLLAMA_MODEL_NAME,
            messages=messages,
            stream=True,
            options=options or {},
        )
        async for chunk in stream:
            content = self._extract_message_content(chunk)
            if content:
                yield content

    async def embeddings(self, text: str) -> list[float]:
        response = await self._client.embed(
            model="nomic-embed-text",
            input=[text],
        )
        embeddings = self._field(response, "embeddings")
        if isinstance(embeddings, list) and embeddings:
            first = embeddings[0]
            if isinstance(first, list):
                return self._normalize_embedding_dim([float(value) for value in first])
            if isinstance(first, (int, float)):
                flat = [float(value) for value in embeddings if isinstance(value, (int, float))]
                return self._normalize_embedding_dim(flat)
        return self._normalize_embedding_dim([])


ollama_client = OllamaClient()
