"""SSRF-protected universal async HTTP client for Dynamic Tool execution.

Every outgoing request goes through:
1. URL scheme validation (http/https only)
2. DNS resolution with private/reserved IP blocking
3. Egress policy enforcement (port allowlist, host deny/allow)
4. Async execution via httpx with timeout

This client MUST be used for all user-controlled URL requests (dynamic
tools, integrations, webhooks) to prevent Server-Side Request Forgery.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.config import settings
from app.services.egress_policy_service import egress_policy_service

logger = logging.getLogger(__name__)

# Maximum response body size to prevent memory exhaustion (10 MB)
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024


def _resolve_and_check_host(hostname: str) -> None:
    """Resolve hostname via DNS and reject private/reserved IPs (SSRF guard).

    Raises ``ValueError`` if any resolved address falls into a
    private, loopback, link-local, reserved, multicast, or unspecified range.
    """
    # Direct IP literal — check immediately
    try:
        addr = ipaddress.ip_address(hostname)
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast or addr.is_unspecified:
            raise ValueError(f"Blocked request to private/reserved IP: {hostname}")
        return
    except ValueError:
        # Not a bare IP — proceed with DNS resolution
        if "Blocked" in str(hostname):
            raise

    try:
        resolved = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        # Unresolvable host — let httpx handle the error downstream
        return

    for family, _type, _proto, _canonname, sockaddr in resolved:
        ip_str = sockaddr[0]
        addr = ipaddress.ip_address(ip_str)
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast or addr.is_unspecified:
            raise ValueError(
                f"Blocked request to {hostname}: resolves to private/reserved IP {ip_str}"
            )


class SafeHttpClient:
    """Universal async HTTP client with SSRF protection.

    Usage::

        result = await safe_http_client.execute(
            method="GET",
            url="https://api.example.com/data",
            params={"q": "hello"},
            timeout=30,
        )
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            limits = httpx.Limits(
                max_connections=max(10, settings.HTTP_CLIENT_MAX_CONNECTIONS),
                max_keepalive_connections=max(5, settings.HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS),
                keepalive_expiry=max(1.0, settings.HTTP_CLIENT_KEEPALIVE_EXPIRY_SECONDS),
            )
            self._client = httpx.AsyncClient(
                limits=limits,
                follow_redirects=False,  # Prevent redirect-based SSRF bypass
            )
        return self._client

    async def execute(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 30.0,
        auth_token: str | None = None,
    ) -> dict[str, Any]:
        """Execute an HTTP request with full SSRF protection.

        Returns dict with ``status_code``, ``headers``, ``body``, ``success``.
        """
        # 1. Validate URL scheme
        safe_url = str(url or "").strip()
        parsed = urlparse(safe_url)
        if parsed.scheme not in ("http", "https"):
            return {"success": False, "error": "Only http/https URLs are allowed"}

        hostname = parsed.hostname or ""
        if not hostname:
            return {"success": False, "error": "Invalid URL: no host"}

        # 2. DNS-level SSRF check (private/reserved IP blocking)
        try:
            _resolve_and_check_host(hostname)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        # 3. Egress policy enforcement (port allowlist, host deny/allow)
        try:
            safe_url = egress_policy_service.validate_url(safe_url)
        except ValueError as exc:
            return {"success": False, "error": f"Egress policy blocked: {exc}"}

        # 4. Build headers
        req_headers = dict(headers or {})
        if auth_token:
            req_headers.setdefault("Authorization", f"Bearer {auth_token}")

        # 5. Execute request
        try:
            client = self._get_client()
            async with asyncio.timeout(min(timeout, 60)):
                response = await client.request(
                    method=method.upper(),
                    url=safe_url,
                    headers=req_headers,
                    params=params,
                    json=json_body,
                    timeout=timeout,
                )
            body = response.text[:_MAX_RESPONSE_BYTES]
            return {
                "success": True,
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "body": body,
            }
        except httpx.TimeoutException:
            return {"success": False, "error": f"Request timed out after {timeout}s"}
        except httpx.ConnectError as exc:
            return {"success": False, "error": f"Connection failed: {exc}"}
        except Exception as exc:
            logger.warning("SafeHttpClient request failed: %s", exc)
            return {"success": False, "error": f"Request failed: {type(exc).__name__}"}

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


safe_http_client = SafeHttpClient()
