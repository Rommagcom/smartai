"""LiteLLM-based unified LLM provider.

Replaces direct Ollama SDK calls with a provider-agnostic interface.
Supports OpenAI, Anthropic, Ollama, and 100+ other models via LiteLLM.
Uses Pydantic v2 for structured output parsing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncGenerator, Type, TypeVar

import httpx
import litellm
from pydantic import BaseModel, ValidationError

from app.core.config import settings
from app.llm.structured_parser import (
    StructuredParseError,
    _is_expected_structured_parse_error,
    _is_tool_payload_schema_mismatch,
    _log_structured_parse_failure,
    parse_structured_response,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Suppress verbose litellm logging
litellm.suppress_debug_info = True
litellm.set_verbose = False


class LLMProvider:
    """Unified LLM interface backed by LiteLLM.

    Usage::

        provider = LLMProvider()
        text = await provider.chat([{"role": "user", "content": "Hello"}])
        parsed = await provider.chat_structured(
            messages=[...],
            response_model=RouterOutput,
        )
    """

    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(max(1, settings.OLLAMA_MAX_CONCURRENCY))

    # ------------------------------------------------------------------
    # Model resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_model(model: str | None) -> str:
        """Resolve model string. If the setting starts with a known prefix
        (openai/, anthropic/, ollama/, etc.) use as-is.
        Otherwise, assume Ollama via the configured base URL.
        """
        # Priority: explicit arg > LITELLM_MODEL > legacy env LLM_MODEL > OLLAMA_MODEL_NAME
        # This keeps backward compatibility with existing .env files where only
        # LLM_MODEL/OLLAMA_MODEL_NAME are defined.
        name = (model or settings.LITELLM_MODEL or "").strip()
        if not name:
            name = os.getenv("LLM_MODEL", "").strip()
        if not name:
            name = (settings.OLLAMA_MODEL_NAME or "").strip()

        # Common Ollama tags look like "model:tag" (e.g. qwen2.5:7b).
        # Treat such bare names as local Ollama models by default.
        if name and "/" not in name and ":" in name:
            return f"ollama_chat/{name}"

        # If it already has a provider prefix, return as-is
        known_prefixes = (
            "openai/", "anthropic/", "ollama/", "ollama_chat/",
            "azure/", "bedrock/", "together_ai/", "groq/",
            "gpt-", "claude-", "o1-", "o3-",
        )
        if any(name.startswith(p) for p in known_prefixes):
            return name

        # Default: wrap as ollama model
        return f"ollama_chat/{name}"

    @staticmethod
    async def _fallback_ollama_model(api_base: str) -> str | None:
        """Return first installed Ollama model name, if any."""
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.get(f"{api_base.rstrip('/')}/api/tags")
                resp.raise_for_status()
            payload = resp.json()
            models = payload.get("models") if isinstance(payload, dict) else None
            if not isinstance(models, list):
                return None
            for item in models:
                if isinstance(item, dict):
                    name = str(item.get("name") or "").strip()
                    if name:
                        return f"ollama_chat/{name}"
        except Exception:
            logger.debug("Failed to query Ollama tags for fallback model", exc_info=True)
        return None

    def _base_params(self, model: str | None = None) -> dict[str, Any]:
        """Build common parameters for litellm calls."""
        resolved = self._resolve_model(model)
        params: dict[str, Any] = {
            "model": resolved,
            "timeout": settings.LITELLM_TIMEOUT_SECONDS,
        }
        # Set API base for Ollama models
        if resolved.startswith("ollama"):
            params["api_base"] = settings.OLLAMA_BASE_URL
            # Enable model reasoning mode for Ollama-backed calls.
            params["think"] = True

        # Set API keys from config if available
        if settings.LITELLM_OPENAI_API_KEY:
            params["api_key"] = settings.LITELLM_OPENAI_API_KEY
        if settings.LITELLM_ANTHROPIC_API_KEY:
            params["api_key"] = settings.LITELLM_ANTHROPIC_API_KEY

        return params

    # ------------------------------------------------------------------
    # Core chat
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        stream: bool = False,
        retries: int = 2,
    ) -> str:
        """Send messages to the LLM and return the text response."""
        params = self._base_params(model)
        params["messages"] = messages
        params["temperature"] = temperature
        if max_tokens:
            params["max_tokens"] = max_tokens
        params["stream"] = False

        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                async with self._semaphore:
                    response = await litellm.acompletion(**params)
                return response.choices[0].message.content or ""
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "LLM chat attempt %d/%d failed: %s",
                    attempt, retries, exc,
                )

                # If configured Ollama model is missing, try first available local model.
                err_text = str(exc).lower()
                current_model = str(params.get("model") or "")
                if (
                    "not found" in err_text
                    and "model" in err_text
                    and current_model.startswith("ollama_chat/")
                    and attempt < retries
                ):
                    fallback_model = await self._fallback_ollama_model(settings.OLLAMA_BASE_URL)
                    if fallback_model and fallback_model != current_model:
                        logger.warning(
                            "Switching to fallback Ollama model: %s -> %s",
                            current_model,
                            fallback_model,
                        )
                        params["model"] = fallback_model
                        params["api_base"] = settings.OLLAMA_BASE_URL
                        continue

                if attempt < retries:
                    await asyncio.sleep(1.0 * attempt)

        raise last_exc  # type: ignore[misc]

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[str, None]:
        """Stream tokens from the LLM."""
        params = self._base_params(model)
        params["messages"] = messages
        params["temperature"] = temperature
        if max_tokens:
            params["max_tokens"] = max_tokens
        params["stream"] = True

        async with self._semaphore:
            response = await litellm.acompletion(**params)

        async for chunk in response:
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield delta.content

    # ------------------------------------------------------------------
    # Structured output (Pydantic v2)
    # ------------------------------------------------------------------

    async def chat_structured(
        self,
        messages: list[dict[str, str]],
        response_model: Type[T],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        retries: int = 2,
    ) -> T:
        """Call LLM and parse the response into a Pydantic model.

        Uses JSON mode + schema enforcement. Falls back to extracting
        JSON from freeform text if strict mode fails.
        """
        schema_json = json.dumps(
            response_model.model_json_schema(),
            ensure_ascii=False,
        )
        schema_instruction = (
            f"You MUST respond with valid JSON matching this schema:\n{schema_json}\n"
            "Return ONLY the JSON object, no markdown fences or extra text."
        )

        # Prepend schema instruction to system message or add as first message
        augmented = list(messages)
        if augmented and augmented[0]["role"] == "system":
            augmented[0] = {
                "role": "system",
                "content": augmented[0]["content"] + "\n\n" + schema_instruction,
            }
        else:
            augmented.insert(0, {"role": "system", "content": schema_instruction})

        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                raw = await self.chat(
                    augmented,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                parsed = parse_structured_response(raw, response_model)
                return parsed
            except (ValidationError, json.JSONDecodeError, StructuredParseError) as exc:
                if _is_tool_payload_schema_mismatch(exc):
                    last_exc = StructuredParseError(
                        "Structured schema mismatch: got tool payload for non-tool response model"
                    )
                    _log_structured_parse_failure(attempt, retries, last_exc)
                    break
                last_exc = exc
                _log_structured_parse_failure(attempt, retries, exc)
                if attempt < retries:
                    await asyncio.sleep(0.1 * attempt)

        raise ValueError(
            f"Failed to parse LLM response into {response_model.__name__} "
            f"after {retries} attempts: {last_exc}"
        )

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    async def embeddings(self, text: str, *, model: str | None = None) -> list[float]:
        """Generate embeddings using LiteLLM."""
        embed_model = model or settings.LITELLM_EMBEDDING_MODEL
        if not embed_model:
            # Fallback to existing Ollama embedding
            from app.services.ollama_client import ollama_client
            return await ollama_client.embeddings(text)

        try:
            async with self._semaphore:
                response = await litellm.aembedding(
                    model=embed_model,
                    input=[text],
                    timeout=settings.LITELLM_TIMEOUT_SECONDS,
                )
            data = response.data
            if data and len(data) > 0:
                return data[0]["embedding"]
            return [0.0] * settings.EMBEDDING_DIM
        except Exception as exc:
            logger.warning("LiteLLM embedding failed: %s, falling back to Ollama", exc)
            from app.services.ollama_client import ollama_client
            return await ollama_client.embeddings(text)


# Singleton
llm_provider = LLMProvider()
