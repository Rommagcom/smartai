"""LiteLLM-based unified LLM provider.

Replaces direct Ollama SDK calls with a provider-agnostic interface.
Supports OpenAI, Anthropic, Ollama, and 100+ other models via LiteLLM.
Uses Pydantic v2 for structured output parsing.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncGenerator, Type, TypeVar

import litellm
from pydantic import BaseModel, ValidationError

from app.core.config import settings

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
        name = (model or settings.LITELLM_MODEL).strip()
        if not name:
            name = settings.LITELLM_MODEL

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
                parsed = self._parse_structured_response(raw, response_model)
                return parsed
            except (ValidationError, json.JSONDecodeError) as exc:
                last_exc = exc
                logger.warning(
                    "structured parse attempt %d/%d failed: %s",
                    attempt, retries, exc,
                )
                if attempt < retries:
                    await asyncio.sleep(0.1 * attempt)

        raise ValueError(
            f"Failed to parse LLM response into {response_model.__name__} "
            f"after {retries} attempts: {last_exc}"
        )

    @staticmethod
    def _parse_structured_response(raw: str, model: Type[T]) -> T:
        """Extract and validate JSON from LLM response text."""
        text = raw.strip()

        # Try direct parse
        try:
            return model.model_validate_json(text)
        except (ValidationError, json.JSONDecodeError):
            pass

        # Strip markdown fences
        import re
        fenced = re.search(r"```(?:json)?\s*\n?([\s\S]*?)```", text, re.IGNORECASE)
        if fenced:
            try:
                return model.model_validate_json(fenced.group(1).strip())
            except (ValidationError, json.JSONDecodeError):
                pass

        # Find first JSON object
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end > brace_start:
            candidate = text[brace_start: brace_end + 1]
            return model.model_validate_json(candidate)

        raise ValueError(f"No valid JSON found in LLM response: {text[:200]}")

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
