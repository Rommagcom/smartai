import asyncio
import logging
from typing import AsyncGenerator

import httpx
from ollama import AsyncClient  # type: ignore[import-not-found]

from app.core.config import settings

logger = logging.getLogger(__name__)


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

    @classmethod
    def _extract_total_tokens(cls, response: object) -> int:
        prompt = cls._field(response, "prompt_eval_count")
        completion = cls._field(response, "eval_count")
        prompt_tokens = int(prompt) if isinstance(prompt, (int, float)) else 0
        completion_tokens = int(completion) if isinstance(completion, (int, float)) else 0
        return max(0, prompt_tokens + completion_tokens)

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

    @staticmethod
    def _is_model_not_found_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return "model" in message and "not found" in message

    async def _discover_fallback_model(self, current_model: str) -> str | None:
        """Pick a local Ollama model if configured model does not exist."""
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.get(f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/tags")
                resp.raise_for_status()
            payload = resp.json()
            models = payload.get("models") if isinstance(payload, dict) else None
            if not isinstance(models, list):
                return None
            for item in models:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if name and name != current_model:
                    return name
        except Exception:
            logger.debug("Failed to query /api/tags for model fallback", exc_info=True)
        return None

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

    @staticmethod
    def _merge_options(options: dict | None, extra: dict | None = None) -> dict:
        merged = dict(options or {})
        if extra:
            for key, value in extra.items():
                if key not in merged:
                    merged[key] = value
        return merged

    async def chat(self, messages: list[dict], stream: bool = False, options: dict | None = None) -> str:
        merged = self._merge_options(options, {"num_predict": settings.OLLAMA_NUM_PREDICT})
        model_name = str(settings.OLLAMA_MODEL_NAME or "").strip()

        async def _chat(model: str):
            return await self._run_with_retry(
                lambda: self._client.chat(
                    model=model,
                    messages=messages,
                    stream=stream,
                    think=True,
                    options=merged,
                    keep_alive=settings.OLLAMA_KEEP_ALIVE,
                )
            )

        try:
            response = await _chat(model_name)
        except Exception as exc:
            if self._is_model_not_found_error(exc):
                if not settings.OLLAMA_MODEL_FALLBACK_ENABLED:
                    logger.error(
                        "Configured Ollama model is missing and fallback is disabled: %s",
                        model_name,
                    )
                    raise
                fallback_model = await self._discover_fallback_model(model_name)
                if fallback_model and fallback_model != model_name:
                    logger.warning("Switching Ollama model fallback: %s -> %s", model_name, fallback_model)
                    response = await _chat(fallback_model)
                else:
                    raise
            else:
                raise
        total_tokens = self._extract_total_tokens(response)
        if total_tokens > 0:
            from app.services.llm_usage_service import llm_usage_service

            llm_usage_service.record_total_tokens(total_tokens)
        return self._extract_message_content(response)

    async def stream_chat(self, messages: list[dict], options: dict | None = None) -> AsyncGenerator[str, None]:
        merged = self._merge_options(options, {"num_predict": settings.OLLAMA_NUM_PREDICT})
        model_name = str(settings.OLLAMA_MODEL_NAME or "").strip()

        async def _chat_stream(model: str):
            return await self._run_with_retry(
                lambda: self._client.chat(
                    model=model,
                    messages=messages,
                    stream=True,
                    think=True,
                    options=merged,
                    keep_alive=settings.OLLAMA_KEEP_ALIVE,
                )
            )

        try:
            stream = await _chat_stream(model_name)
        except Exception as exc:
            if self._is_model_not_found_error(exc):
                if not settings.OLLAMA_MODEL_FALLBACK_ENABLED:
                    logger.error(
                        "Configured Ollama stream model is missing and fallback is disabled: %s",
                        model_name,
                    )
                    raise
                fallback_model = await self._discover_fallback_model(model_name)
                if fallback_model and fallback_model != model_name:
                    logger.warning("Switching Ollama stream model fallback: %s -> %s", model_name, fallback_model)
                    stream = await _chat_stream(fallback_model)
                else:
                    raise
            else:
                raise

        stream_total_tokens = 0
        async for chunk in stream:
            chunk_total = self._extract_total_tokens(chunk)
            if chunk_total > stream_total_tokens:
                stream_total_tokens = chunk_total
            content = self._extract_message_content(chunk)
            if content:
                yield content

        if stream_total_tokens > 0:
            from app.services.llm_usage_service import llm_usage_service

            llm_usage_service.record_total_tokens(stream_total_tokens)

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
                import logging as _logging
                _logging.getLogger(__name__).warning("embedding rate-limited, returning zero vector")
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
