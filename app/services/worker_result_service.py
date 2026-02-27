from __future__ import annotations

import asyncio
from collections import defaultdict, deque
import json

from redis.asyncio import Redis

from app.core.config import settings


class WorkerResultService:
    def __init__(self) -> None:
        self._results: dict[str, deque[dict]] = defaultdict(deque)
        self._redis: Redis | None = None

    def _get_redis(self) -> Redis:
        if self._redis is None:
            self._redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        return self._redis

    @staticmethod
    def _key(user_id: str) -> str:
        return f"{settings.WORKER_RESULT_QUEUE_PREFIX}:{user_id}"

    async def push(self, user_id: str, payload: dict) -> None:
        try:
            redis = self._get_redis()
            key = self._key(user_id)
            max_items = max(10, int(settings.WORKER_RESULT_QUEUE_MAX_ITEMS))
            await asyncio.wait_for(redis.rpush(key, json.dumps(payload, ensure_ascii=False)), timeout=0.5)
            await asyncio.wait_for(redis.ltrim(key, -max_items, -1), timeout=0.5)
            await asyncio.wait_for(redis.expire(key, max(60, int(settings.WORKER_RESULT_TTL_SECONDS))), timeout=0.5)
            return
        except Exception:
            self._results[user_id].append(payload)

    async def pop_many(self, user_id: str, limit: int = 20) -> list[dict]:
        count = max(1, min(limit, 100))
        try:
            redis = self._get_redis()
            key = self._key(user_id)
            raw_items = await asyncio.wait_for(redis.lrange(key, 0, count - 1), timeout=0.5)
            if raw_items:
                await asyncio.wait_for(redis.ltrim(key, count, -1), timeout=0.5)
                items: list[dict] = []
                for raw in raw_items:
                    try:
                        payload = json.loads(raw)
                        if isinstance(payload, dict):
                            items.append(payload)
                    except (TypeError, ValueError):
                        continue
                return items
        except Exception:
            fallback_items = self._pop_many_in_memory(user_id=user_id, limit=count)
            return fallback_items

        queue = self._results.get(user_id)
        if not queue:
            return []

        items: list[dict] = []
        for _ in range(count):
            if not queue:
                break
            items.append(queue.popleft())

        if not queue:
            self._results.pop(user_id, None)
        return items

    async def clear_user_results(self, user_id: str) -> None:
        try:
            redis = self._get_redis()
            await asyncio.wait_for(redis.delete(self._key(user_id)), timeout=0.5)
        except Exception:
            self._results.pop(user_id, None)
            return
        self._results.pop(user_id, None)

    def _pop_many_in_memory(self, user_id: str, limit: int) -> list[dict]:
        queue = self._results.get(user_id)
        if not queue:
            return []

        count = max(1, min(limit, 100))
        items: list[dict] = []
        for _ in range(count):
            if not queue:
                break
            items.append(queue.popleft())

        if not queue:
            self._results.pop(user_id, None)
        return items


worker_result_service = WorkerResultService()
