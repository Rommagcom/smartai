from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Protocol

from redis.asyncio import Redis

from search_agent.config import Settings

logger = logging.getLogger(__name__)


HistoryMessage = dict[str, str]
UsageStats = dict[str, int]


class ConversationStore(Protocol):
    async def get_history(self, chat_id: int) -> list[HistoryMessage]:
        ...

    async def append_turn(self, chat_id: int, user_text: str, assistant_text: str) -> None:
        ...

    async def clear_chat(self, chat_id: int) -> None:
        ...

    async def add_token_usage(
        self,
        chat_id: int,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        requests: int,
    ) -> None:
        ...

    async def get_token_usage(self, chat_id: int) -> UsageStats:
        ...

    async def close(self) -> None:
        ...


class InMemoryConversationStore:
    def __init__(self, max_messages: int) -> None:
        self.max_messages = max_messages
        self._history: dict[int, list[HistoryMessage]] = defaultdict(list)
        self._usage: dict[int, UsageStats] = defaultdict(
            lambda: {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "requests": 0,
            }
        )

    async def get_history(self, chat_id: int) -> list[HistoryMessage]:
        return list(self._history.get(chat_id, []))

    async def append_turn(self, chat_id: int, user_text: str, assistant_text: str) -> None:
        messages = self._history.get(chat_id, [])
        messages.append({"role": "user", "content": user_text})
        messages.append({"role": "assistant", "content": assistant_text})
        if len(messages) > self.max_messages:
            messages = messages[-self.max_messages :]
        self._history[chat_id] = messages

    async def clear_chat(self, chat_id: int) -> None:
        self._history.pop(chat_id, None)
        self._usage.pop(chat_id, None)

    async def add_token_usage(
        self,
        chat_id: int,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        requests: int,
    ) -> None:
        usage = self._usage[chat_id]
        usage["prompt_tokens"] += int(prompt_tokens)
        usage["completion_tokens"] += int(completion_tokens)
        usage["total_tokens"] += int(total_tokens)
        usage["requests"] += int(requests)

    async def get_token_usage(self, chat_id: int) -> UsageStats:
        usage = self._usage.get(chat_id)
        if not usage:
            return {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "requests": 0,
            }
        return dict(usage)

    async def close(self) -> None:
        return


class RedisConversationStore:
    def __init__(
        self,
        client: Redis,
        key_prefix: str,
        max_messages: int,
        ttl_seconds: int,
    ) -> None:
        self.client = client
        self.key_prefix = key_prefix
        self.max_messages = max_messages
        self.ttl_seconds = ttl_seconds

    def _key(self, chat_id: int) -> str:
        return f"{self.key_prefix}:{chat_id}"

    def _usage_key(self, chat_id: int) -> str:
        return f"{self.key_prefix}:usage:{chat_id}"

    async def get_history(self, chat_id: int) -> list[HistoryMessage]:
        key = self._key(chat_id)
        raw_items = await self.client.lrange(key, 0, -1)
        history: list[HistoryMessage] = []

        for item in raw_items:
            try:
                parsed = json.loads(item)
            except json.JSONDecodeError:
                continue

            role = str(parsed.get("role", "")).strip()
            content = str(parsed.get("content", ""))
            if role not in {"user", "assistant"}:
                continue
            history.append({"role": role, "content": content})

        if len(history) > self.max_messages:
            history = history[-self.max_messages :]
        return history

    async def append_turn(self, chat_id: int, user_text: str, assistant_text: str) -> None:
        key = self._key(chat_id)
        user_msg = json.dumps({"role": "user", "content": user_text}, ensure_ascii=True)
        assistant_msg = json.dumps({"role": "assistant", "content": assistant_text}, ensure_ascii=True)

        pipe = self.client.pipeline()
        pipe.rpush(key, user_msg, assistant_msg)
        pipe.ltrim(key, -self.max_messages, -1)
        if self.ttl_seconds > 0:
            pipe.expire(key, self.ttl_seconds)
        await pipe.execute()

    async def clear_chat(self, chat_id: int) -> None:
        await self.client.delete(self._key(chat_id), self._usage_key(chat_id))

    async def add_token_usage(
        self,
        chat_id: int,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        requests: int,
    ) -> None:
        usage_key = self._usage_key(chat_id)
        pipe = self.client.pipeline()
        pipe.hincrby(usage_key, "prompt_tokens", int(prompt_tokens))
        pipe.hincrby(usage_key, "completion_tokens", int(completion_tokens))
        pipe.hincrby(usage_key, "total_tokens", int(total_tokens))
        pipe.hincrby(usage_key, "requests", int(requests))
        if self.ttl_seconds > 0:
            pipe.expire(usage_key, self.ttl_seconds)
        await pipe.execute()

    async def get_token_usage(self, chat_id: int) -> UsageStats:
        usage_key = self._usage_key(chat_id)
        usage_raw = await self.client.hgetall(usage_key)
        return {
            "prompt_tokens": int(usage_raw.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage_raw.get("completion_tokens", 0) or 0),
            "total_tokens": int(usage_raw.get("total_tokens", 0) or 0),
            "requests": int(usage_raw.get("requests", 0) or 0),
        }

    async def close(self) -> None:
        await self.client.aclose()


async def build_conversation_store(settings: Settings) -> ConversationStore:
    if not settings.redis_url:
        return InMemoryConversationStore(max_messages=settings.max_conversation_messages)

    try:
        client = Redis.from_url(settings.redis_url, decode_responses=True)
        await client.ping()
    except Exception as exc:
        logger.warning("Redis unavailable (%s). Falling back to in-memory conversation store.", exc)
        return InMemoryConversationStore(max_messages=settings.max_conversation_messages)

    return RedisConversationStore(
        client=client,
        key_prefix=settings.redis_key_prefix,
        max_messages=settings.max_conversation_messages,
        ttl_seconds=settings.redis_conversation_ttl_seconds,
    )
