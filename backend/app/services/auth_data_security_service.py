from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


class AuthDataSecurityService:
    _ENC_MARKER = "__enc_v1"
    _ENC_KID = "kid"

    @staticmethod
    def _parse_key_item(index: int, raw_item: str) -> tuple[str, str] | None:
        value = str(raw_item or "").strip()
        if not value:
            return None

        if ":" in value:
            key_id, key_raw = value.split(":", 1)
            kid = str(key_id or "").strip() or f"k{index}"
            key_value = str(key_raw or "").strip()
        else:
            kid = f"k{index}"
            key_value = value

        if not key_value:
            return None
        return kid, key_value

    def _keyring_from_settings(self) -> dict[str, Fernet]:
        keyring: dict[str, Fernet] = {}
        raw_keys = str(settings.AUTH_DATA_ENCRYPTION_KEYS or "").strip()
        if not raw_keys:
            return keyring

        for index, item in enumerate(raw_keys.split(","), start=1):
            parsed = self._parse_key_item(index, item)
            if not parsed:
                continue
            kid, key_value = parsed
            keyring[kid] = Fernet(key_value.encode("utf-8"))
        return keyring

    def _build_keyring(self) -> tuple[str, dict[str, Fernet]]:
        keyring = self._keyring_from_settings()

        if not keyring:
            derived = base64.urlsafe_b64encode(hashlib.sha256(settings.JWT_SECRET_KEY.encode("utf-8")).digest())
            keyring["jwt-derived"] = Fernet(derived)

        active_kid = str(settings.AUTH_DATA_ACTIVE_KEY_ID or "").strip()
        if not active_kid or active_kid not in keyring:
            active_kid = next(iter(keyring.keys()))

        return active_kid, keyring

    def encrypt(self, auth_data: dict) -> dict:
        payload = dict(auth_data or {})
        if not payload:
            return {}

        active_kid, keyring = self._build_keyring()
        fernet = keyring[active_kid]
        token = fernet.encrypt(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        return {
            self._ENC_MARKER: token.decode("utf-8"),
            self._ENC_KID: active_kid,
        }

    def resolve_for_runtime(self, stored_auth_data: Any) -> tuple[dict, dict | None]:
        payload = stored_auth_data if isinstance(stored_auth_data, dict) else {}
        if not payload:
            return {}, None

        active_kid, keyring = self._build_keyring()
        encrypted = str(payload.get(self._ENC_MARKER) or "").strip()
        if not encrypted:
            return dict(payload), self.encrypt(payload)

        preferred_kid = str(payload.get(self._ENC_KID) or "").strip()
        key_order: list[tuple[str, Fernet]] = []
        if preferred_kid and preferred_kid in keyring:
            key_order.append((preferred_kid, keyring[preferred_kid]))
        key_order.extend((kid, key) for kid, key in keyring.items() if kid != preferred_kid)

        for kid, fernet in key_order:
            try:
                decoded = fernet.decrypt(encrypted.encode("utf-8")).decode("utf-8")
            except InvalidToken:
                continue

            parsed = json.loads(decoded)
            auth_data = parsed if isinstance(parsed, dict) else {}
            rotated = self.encrypt(auth_data) if kid != active_kid else None
            return auth_data, rotated

        raise ValueError("Failed to decrypt integration auth_data with configured keyring")


auth_data_security_service = AuthDataSecurityService()
