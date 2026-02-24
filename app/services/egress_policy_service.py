from __future__ import annotations

import ipaddress
import socket
from fnmatch import fnmatch
from urllib.parse import urlparse

from app.core.config import settings


class EgressPolicyService:
    @staticmethod
    def _parse_csv(value: str) -> list[str]:
        return [item.strip().lower() for item in str(value or "").split(",") if item.strip()]

    @staticmethod
    def _parse_ports(value: str) -> set[int]:
        ports: set[int] = set()
        for item in str(value or "").split(","):
            raw = str(item or "").strip()
            if not raw:
                continue
            try:
                port = int(raw)
            except ValueError:
                continue
            if 1 <= port <= 65535:
                ports.add(port)
        return ports

    @staticmethod
    def _match_host(host: str, patterns: list[str]) -> bool:
        host_normalized = str(host or "").strip().lower()
        if not host_normalized:
            return False
        return any(fnmatch(host_normalized, pattern) for pattern in patterns)

    @staticmethod
    def _ip_is_private(ip_text: str) -> bool:
        ip_obj = ipaddress.ip_address(ip_text)
        return bool(
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_reserved
            or ip_obj.is_multicast
            or ip_obj.is_unspecified
        )

    def _ensure_host_is_not_private(self, host: str) -> None:
        try:
            ipaddress.ip_address(host)
            if self._ip_is_private(host):
                raise ValueError("Egress policy blocked private target")
            return
        except ipaddress.ValueError:
            pass

        try:
            addresses = socket.getaddrinfo(host, None)
        except socket.gaierror:
            return

        for item in addresses:
            ip_value = item[4][0]
            if self._ip_is_private(ip_value):
                raise ValueError("Egress policy blocked private target")

    @staticmethod
    def _extract_host_port(parsed) -> tuple[str, int]:
        host = str(parsed.hostname or "").strip().lower()
        if not host:
            raise ValueError("Invalid URL host")
        port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
        return host, port

    def _enforce_host_port_policy(self, *, host: str, port: int) -> None:
        allowed_ports = self._parse_ports(settings.SANDBOX_EGRESS_ALLOWED_PORTS)
        if allowed_ports and port not in allowed_ports:
            raise ValueError("Egress policy blocked target port")

        denied_hosts = self._parse_csv(settings.SANDBOX_EGRESS_DENIED_HOSTS)
        if self._match_host(host, denied_hosts):
            raise ValueError("Egress policy blocked denied host")

        allowed_hosts = self._parse_csv(settings.SANDBOX_EGRESS_ALLOWED_HOSTS)
        if settings.SANDBOX_EGRESS_ALLOWLIST_MODE and not self._match_host(host, allowed_hosts):
            raise ValueError("Egress policy blocked host not in allowlist")

        if settings.SANDBOX_EGRESS_BLOCK_PRIVATE_NETWORKS:
            self._ensure_host_is_not_private(host)

    def validate_url(self, url: str) -> str:
        normalized_url = str(url or "").strip()
        parsed = urlparse(normalized_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Only http/https URLs are allowed")
        if not parsed.netloc:
            raise ValueError("Invalid URL")
        validated_url = parsed.geturl()

        if settings.SANDBOX_EGRESS_ENABLED:
            host, port = self._extract_host_port(parsed)
            self._enforce_host_port_policy(host=host, port=port)

        return validated_url


egress_policy_service = EgressPolicyService()
