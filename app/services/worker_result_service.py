from __future__ import annotations

from collections import defaultdict, deque


class WorkerResultService:
    def __init__(self) -> None:
        self._results: dict[str, deque[dict]] = defaultdict(deque)

    def push(self, user_id: str, payload: dict) -> None:
        self._results[user_id].append(payload)

    def pop_many(self, user_id: str, limit: int = 20) -> list[dict]:
        queue = self._results.get(user_id)
        if not queue:
            return []

        items: list[dict] = []
        count = max(1, min(limit, 100))
        for _ in range(count):
            if not queue:
                break
            items.append(queue.popleft())

        if not queue:
            self._results.pop(user_id, None)
        return items


worker_result_service = WorkerResultService()
