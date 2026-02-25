import asyncio
import importlib
from collections import defaultdict
from pathlib import Path
from time import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.user import User
from app.models.worker_task import WorkerTask
from app.services.tool_orchestrator_service import tool_orchestrator_service
from app.services.worker_result_service import worker_result_service
from app.workers.models import WorkerJobStatus, WorkerJobType
from app.workers.worker_service import worker_service

worker_module = importlib.import_module("app.workers.worker_service")

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "smoke_worker_queue.db"


class MockRedisPipeline:
    def __init__(self, redis: "MockRedis") -> None:
        self.redis = redis
        self.operations: list[tuple[str, str, str | None]] = []

    def zrem(self, key: str, member: str):
        self.operations.append(("zrem", key, member))
        return self

    def lpush(self, key: str, value: str):
        self.operations.append(("lpush", key, value))
        return self

    async def execute(self):
        await asyncio.sleep(0)
        for op, key, value in self.operations:
            if op == "zrem" and value is not None:
                await self.redis.zrem(key, value)
            if op == "lpush" and value is not None:
                await self.redis.lpush(key, value)
        self.operations.clear()


class MockRedis:
    def __init__(self) -> None:
        self.queues: dict[str, list[str]] = defaultdict(list)
        self.zsets: dict[str, dict[str, float]] = defaultdict(dict)

    async def lpush(self, key: str, value: str) -> int:
        await asyncio.sleep(0)
        self.queues[key].insert(0, value)
        return len(self.queues[key])

    async def brpop(self, key: str, **kwargs):
        timeout_seconds = int(kwargs.get("timeout", 0) or 0)
        queue = self.queues.get(key) or []
        if not queue:
            await asyncio.sleep(max(0, min(timeout_seconds, 1)))
            return None
        value = queue.pop()
        return key, value

    async def brpoplpush(self, source: str, destination: str, **kwargs):
        queue = self.queues.get(source) or []
        timeout_seconds = int(kwargs.get("timeout", 0) or 0)
        if not queue:
            await asyncio.sleep(max(0, min(timeout_seconds, 1)))
            return None
        value = queue.pop()
        self.queues[destination].insert(0, value)
        return value

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        await asyncio.sleep(0)
        queue = self.queues.get(key) or []
        if not queue:
            return []
        end = None if stop < 0 else stop + 1
        return list(queue[start:end])

    async def lrem(self, key: str, count: int, value: str) -> int:
        await asyncio.sleep(0)
        queue = self.queues.get(key) or []
        if not queue:
            return 0

        if count == 0:
            removed, retained = self._remove_all(queue, value)
            self.queues[key] = retained
            return removed

        if count > 0:
            removed, retained = self._remove_from_left(queue, value, count)
            self.queues[key] = retained
            return removed

        removed, retained = self._remove_from_right(queue, value, abs(count))
        self.queues[key] = retained
        return removed

    @staticmethod
    def _remove_all(queue: list[str], value: str) -> tuple[int, list[str]]:
        retained = [item for item in queue if item != value]
        removed = len(queue) - len(retained)
        return removed, retained

    @staticmethod
    def _remove_from_left(queue: list[str], value: str, count: int) -> tuple[int, list[str]]:
        removed = 0
        retained: list[str] = []
        remaining = count
        for item in queue:
            if item == value and remaining > 0:
                removed += 1
                remaining -= 1
                continue
            retained.append(item)
        return removed, retained

    @staticmethod
    def _remove_from_right(queue: list[str], value: str, count: int) -> tuple[int, list[str]]:
        removed = 0
        reversed_items = list(reversed(queue))
        retained_reversed: list[str] = []
        remaining = count
        for item in reversed_items:
            if item == value and remaining > 0:
                removed += 1
                remaining -= 1
                continue
            retained_reversed.append(item)
        return removed, list(reversed(retained_reversed))

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        await asyncio.sleep(0)
        for member, score in mapping.items():
            self.zsets[key][member] = float(score)
        return len(mapping)

    async def zrangebyscore(self, key: str, min: float, max: float, start: int = 0, num: int = 100) -> list[str]:
        await asyncio.sleep(0)
        members = [
            member
            for member, score in sorted(self.zsets.get(key, {}).items(), key=lambda item: item[1])
            if float(min) <= score <= float(max)
        ]
        return members[start : start + num]

    async def zrem(self, key: str, member: str) -> int:
        await asyncio.sleep(0)
        existed = 1 if member in self.zsets.get(key, {}) else 0
        self.zsets[key].pop(member, None)
        return existed

    def pipeline(self) -> MockRedisPipeline:
        return MockRedisPipeline(self)


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def init_db() -> tuple[async_sessionmaker[AsyncSession], object]:
    if DB_PATH.exists():
        DB_PATH.unlink()

    engine = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(User.__table__.create)
        await conn.run_sync(WorkerTask.__table__.create)

    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False), engine


async def create_user(session_factory: async_sessionmaker[AsyncSession]) -> User:
    async with session_factory() as db:
        generated_hash = f"smoke-{time()}"
        user = User(
            username="worker_smoke_user",
            hashed_password=generated_hash,
            soul_configured=True,
            preferences={},
            soul_profile={},
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


async def run() -> None:
    session_factory, engine = await init_db()

    original_session_local = worker_module.AsyncSessionLocal
    original_redis = worker_service._redis
    worker_module.AsyncSessionLocal = session_factory
    worker_service._redis = MockRedis()
    worker_result_service._results.clear()  # type: ignore[attr-defined]

    user = await create_user(session_factory)

    flaky_state = {"attempts": 0}

    async def flaky_web_fetch(payload: dict) -> dict:
        await asyncio.sleep(0)
        flaky_state["attempts"] += 1
        if flaky_state["attempts"] == 1:
            raise RuntimeError("planned retry")
        return {"url": payload.get("url", ""), "content": "ok-after-retry"}

    async def always_fail(payload: dict) -> dict:
        await asyncio.sleep(0)
        del payload
        raise RuntimeError("planned final failure")

    worker_service.register_handler(WorkerJobType.WEB_FETCH, flaky_web_fetch)
    worker_service.register_handler(WorkerJobType.WEB_SEARCH, always_fail)

    try:
        async with session_factory() as db:
            queued = await tool_orchestrator_service._worker_enqueue(  # noqa: SLF001
                db,
                user,
                {
                    "job_type": "web_fetch",
                    "payload": {"url": "https://example.com/retry"},
                },
            )
            ensure(queued.get("status") == "queued", f"expected queued, got: {queued}")

            dedup = await tool_orchestrator_service._worker_enqueue(  # noqa: SLF001
                db,
                user,
                {
                    "job_type": "web_fetch",
                    "payload": {"url": "https://example.com/retry"},
                },
            )
            ensure(dedup.get("status") == "deduplicated", f"expected deduplicated, got: {dedup}")

        first_run = await worker_service.run_once()
        ensure(first_run is not None, "expected first task execution")

        async with session_factory() as db:
            task_row = (
                await db.execute(
                    select(WorkerTask)
                    .where(WorkerTask.job_type == WorkerJobType.WEB_FETCH.value)
                    .order_by(WorkerTask.created_at.desc())
                    .limit(1)
                )
            ).scalar_one()
            ensure(task_row.status == WorkerJobStatus.RETRY_SCHEDULED.value, f"expected retry_scheduled, got: {task_row.status}")

            retry_key = task_row.id

        redis: MockRedis = worker_service._redis  # type: ignore[assignment]
        redis.zsets[worker_module.settings.WORKER_RETRY_ZSET_KEY][str(retry_key)] = time() - 1

        second_run = await worker_service.run_once()
        ensure(second_run is not None, "expected retried task execution")

        success_items = await worker_result_service.pop_many(user_id=str(user.id), limit=10)
        ensure(any(item.get("success") is True for item in success_items), f"expected success event, got: {success_items}")
        ensure(any("result_preview" in item for item in success_items), f"expected result_preview in success event, got: {success_items}")
        ensure(any("next_action_hint" in item for item in success_items), f"expected next_action_hint in success event, got: {success_items}")

        fail_enqueue = await worker_service.enqueue(
            job_type=WorkerJobType.WEB_SEARCH,
            payload={"query": "force fail", "__user_id": str(user.id)},
            max_retries=0,
        )
        ensure(bool(fail_enqueue.get("enqueued")), f"expected enqueued fail task, got: {fail_enqueue}")

        failed_run = await worker_service.run_once()
        ensure(failed_run is not None, "expected failed task execution")

        failed_items = await worker_result_service.pop_many(user_id=str(user.id), limit=10)
        ensure(any(item.get("success") is False for item in failed_items), f"expected failed event, got: {failed_items}")
        ensure(any(isinstance(item.get("error"), dict) and item.get("error", {}).get("message") for item in failed_items), f"expected error.message in failed event, got: {failed_items}")

        print("SMOKE_WORKER_QUEUE_OK")
    finally:
        worker_module.AsyncSessionLocal = original_session_local
        worker_service._redis = original_redis
        await engine.dispose()
        if DB_PATH.exists():
            DB_PATH.unlink()


if __name__ == "__main__":
    asyncio.run(run())
