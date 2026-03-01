"""Lightweight in-memory rate-limiting middleware.

Uses a per-IP sliding-window counter. No external dependencies required.
Configure via settings:
- RATE_LIMIT_ENABLED: bool (default True)
- RATE_LIMIT_REQUESTS_PER_MINUTE: int (default 60)
- RATE_LIMIT_AUTH_REQUESTS_PER_MINUTE: int (default 10)
"""

from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class _SlidingWindowCounter:
    """Per-key sliding-window rate limiter."""

    def __init__(self, window_seconds: int = 60) -> None:
        self._window = window_seconds
        self._lock = Lock()
        self._hits: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, key: str, max_requests: int) -> tuple[bool, int]:
        """Return (allowed, remaining) for the given key."""
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            timestamps = self._hits[key]
            # Evict expired entries
            timestamps[:] = [t for t in timestamps if t > cutoff]
            if len(timestamps) >= max_requests:
                return False, 0
            timestamps.append(now)
            return True, max(0, max_requests - len(timestamps))

    def cleanup(self, max_age: float = 120.0) -> None:
        """Remove stale keys to prevent unbounded memory growth."""
        now = time.monotonic()
        cutoff = now - max_age
        with self._lock:
            stale_keys = [k for k, ts in self._hits.items() if not ts or ts[-1] < cutoff]
            for k in stale_keys:
                del self._hits[k]


# Auth-sensitive paths with stricter limits
_AUTH_PATHS = frozenset({"/api/v1/auth/login", "/api/v1/auth/register", "/api/v1/auth/refresh"})

_general_limiter = _SlidingWindowCounter(window_seconds=60)
_auth_limiter = _SlidingWindowCounter(window_seconds=60)
_last_cleanup = time.monotonic()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        enabled: bool = True,
        requests_per_minute: int = 60,
        auth_requests_per_minute: int = 10,
    ) -> None:
        super().__init__(app)
        self.enabled = enabled
        self.requests_per_minute = requests_per_minute
        self.auth_requests_per_minute = auth_requests_per_minute

    async def dispatch(self, request: Request, call_next: Callable):
        if not self.enabled:
            return await call_next(request)

        global _last_cleanup
        now = time.monotonic()
        if now - _last_cleanup > 120:
            _general_limiter.cleanup()
            _auth_limiter.cleanup()
            _last_cleanup = now

        ip = _client_ip(request)
        path = request.url.path.rstrip("/")

        if path in _AUTH_PATHS:
            allowed, remaining = _auth_limiter.is_allowed(f"auth:{ip}", self.auth_requests_per_minute)
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many authentication attempts. Try again later."},
                    headers={"Retry-After": "60"},
                )
        
        allowed, remaining = _general_limiter.is_allowed(ip, self.requests_per_minute)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Try again later."},
                headers={"Retry-After": "60"},
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
