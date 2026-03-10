import time
from collections import defaultdict
from dataclasses import dataclass
from threading import Lock

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response


@dataclass
class _Bucket:
    minute: int
    count: int = 0


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory per-minute limiter keyed by client IP and auth state."""

    def __init__(
        self,
        app,
        enabled: bool = True,
        requests_per_minute: int = 60,
        auth_requests_per_minute: int = 10,
    ) -> None:
        super().__init__(app)
        self.enabled = enabled
        self.requests_per_minute = max(1, requests_per_minute)
        self.auth_requests_per_minute = max(1, auth_requests_per_minute)
        self._buckets: dict[str, _Bucket] = defaultdict(lambda: _Bucket(minute=-1, count=0))
        self._lock = Lock()

    async def dispatch(self, request: Request, call_next) -> Response:
        if not self.enabled:
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        path = request.url.path.lower()
        is_auth_route = path.startswith("/api/v1/auth")
        limit = self.auth_requests_per_minute if is_auth_route else self.requests_per_minute
        key = f"{client_ip}:{'auth' if is_auth_route else 'general'}"

        allowed, retry_after = self._try_consume(key, limit)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded"},
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)

    def _try_consume(self, key: str, limit: int) -> tuple[bool, int]:
        now = time.time()
        minute = int(now // 60)
        retry_after = max(1, int(60 - (now % 60)))

        with self._lock:
            bucket = self._buckets[key]
            if bucket.minute != minute:
                bucket.minute = minute
                bucket.count = 0

            if bucket.count >= limit:
                return False, retry_after

            bucket.count += 1

        return True, retry_after
