import asyncio
from typing import AsyncGenerator

from ollama import AsyncClient  # type: ignore[import-not-found]

from app.core.config import settings


class OllamaClient:
    def __init__(self) -> None:
        self._client = AsyncClient(host=settings.OLLAMA_BASE_URL)
        self._request_semaphore = asyncio.Semaphore(max(1, int(settings.OLLAMA_MAX_CONCURRENCY)))

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

    @staticmethod
    def _is_rate_limited_error(exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None)
        if status_code == 429:
            return True
        message = str(exc)
        return "429" in message or "Too Many Requests" in message

    async def _run_with_retry(self, request_factory):
        attempts = max(1, int(settings.OLLAMA_RETRY_ATTEMPTS))
        base_delay = max(0.05, float(settings.OLLAMA_RETRY_BASE_DELAY_SECONDS))

        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                async with self._request_semaphore:
                    return await asyncio.wait_for(request_factory(), timeout=settings.OLLAMA_TIMEOUT_SECONDS)
            except Exception as exc:
                last_exc = exc
                is_retryable = self._is_rate_limited_error(exc)
                if not is_retryable or attempt >= attempts:
                    raise
                await asyncio.sleep(base_delay * attempt)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Ollama call failed without exception")

    async def chat(self, messages: list[dict], stream: bool = False, options: dict | None = None) -> str:
        response = await self._run_with_retry(
            lambda: self._client.chat(
                model=settings.OLLAMA_MODEL_NAME,
                messages=messages,
                stream=stream,
                options=options or {},
            )
        )
        return self._extract_message_content(response)

    async def stream_chat(self, messages: list[dict], options: dict | None = None) -> AsyncGenerator[str, None]:
        stream = await self._run_with_retry(
            lambda: self._client.chat(
                model=settings.OLLAMA_MODEL_NAME,
                messages=messages,
                stream=True,
                options=options or {},
            )
        )
        async for chunk in stream:
            content = self._extract_message_content(chunk)
            if content:
                yield content

    async def embeddings(self, text: str) -> list[float]:
        try:
            response = await self._run_with_retry(
                lambda: self._client.embed(
                model="nomic-embed-text",
                input=[text],
                )
            )
        except Exception as exc:
            if self._is_rate_limited_error(exc):
                return self._normalize_embedding_dim([])
            raise
        raw_embeddings = self._field(response, "embeddings")
        if not isinstance(raw_embeddings, list) or not raw_embeddings:
            return self._normalize_embedding_dim([])

        embeddings_list = raw_embeddings
        first = embeddings_list[0]
        if isinstance(first, list):
            return self._normalize_embedding_dim([float(value) for value in first])
        if isinstance(first, (int, float)):
            flat = [float(value) for value in embeddings_list if isinstance(value, (int, float))]
            return self._normalize_embedding_dim(flat)
        return self._normalize_embedding_dim([])


ollama_client = OllamaClient()
