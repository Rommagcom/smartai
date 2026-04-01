from __future__ import annotations

import asyncio
import contextlib
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import zlib
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, NoReturn, Sequence

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError
from redis import asyncio as redis_async
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from search_agent.agent.graph import OllamaLangGraphAgent
from search_agent.config import Settings, load_settings
from search_agent.memory.long_term import LongTermMemoryStore
from search_agent.security.rbac import RbacStore, Role


APP_NAME = "SmartAi API"
TOKEN_TTL_HOURS = 24
PBKDF2_ITERATIONS = 120_000
GROUP_SHORT_MEMORY_LIMIT = 20
USER_SCOPE_ORG_ID = "user"
_SKILL_NAME_RE = re.compile(r"[^a-z0-9_]+")


def _resolve_db_url(explicit: str | None = None) -> str:
    value = (explicit or os.getenv("REMINDER_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
    if value.startswith("postgres://"):
        return "postgresql+psycopg://" + value.removeprefix("postgres://")
    if value.startswith("postgresql://"):
        return "postgresql+psycopg://" + value.removeprefix("postgresql://")
    return value


def _hash_password(password: str, salt: str | None = None) -> str:
    use_salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        use_salt.encode("utf-8"),
        PBKDF2_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${use_salt}${digest}"


def _verify_password(password: str, stored: str) -> bool:
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    _, raw_iters, salt, expected = parts
    try:
        iterations = int(raw_iters)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations).hex()
    return secrets.compare_digest(digest, expected)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _scope_chat_id(user_id: int) -> int:
    key = f"user:{max(0, int(user_id))}"
    return int(zlib.crc32(key.encode("utf-8")) & 0x7FFFFFFF)


def _user_scope_id(user_id: int) -> str:
    normalized = max(0, int(user_id))
    return f"user:{normalized}"


def _slugify_skill_name(raw: str) -> str:
    lowered = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    cleaned = _SKILL_NAME_RE.sub("_", lowered).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)
    if not cleaned:
        raise ValueError("Skill name is empty after normalization")
    if cleaned[0].isdigit():
        cleaned = f"skill_{cleaned}"
    return cleaned


def _parse_claude_markdown(content: str) -> tuple[dict[str, str], str]:
    text = (content or "").replace("\r\n", "\n").lstrip("\ufeff")
    if not text.strip():
        raise ValueError("Markdown content is empty")

    meta: dict[str, str] = {}
    body = text
    if text.startswith("---\n"):
        marker = "\n---\n"
        end = text.find(marker, 4)
        if end == -1:
            raise ValueError("Frontmatter is not closed with ---")
        frontmatter = text[4:end]
        body = text[end + len(marker) :]
        for raw_line in frontmatter.splitlines():
            line = raw_line.strip()
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            k = key.strip().lower()
            v = value.strip().strip('"').strip("'")
            if k:
                meta[k] = v

    return meta, body.strip()


def _decode_uploaded_markdown(raw_bytes: bytes) -> str:
    if not raw_bytes:
        raise ValueError("Uploaded file is empty")

    for encoding in ("utf-8", "utf-8-sig", "cp1251"):
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue

    raise ValueError("Unable to decode file content")


def _build_generated_tool_py(*, function_name: str, tool_name: str, instruction_text: str) -> str:
    escaped_instruction = json.dumps(instruction_text, ensure_ascii=True)
    return (
        "from __future__ import annotations\n\n"
        "import json\n\n"
        f"INSTRUCTION_TEXT = {escaped_instruction}\n\n"
        f"def {function_name}(request: str, context: str = \"\") -> str:\n"
        "    request_text = str(request or \"\").strip()\n"
        "    if not request_text:\n"
        "        raise ValueError(\"request is required\")\n"
        "\n"
        "    payload = {\n"
        f"        \"skill\": \"{tool_name}\",\n"
        "        \"request\": request_text,\n"
        "        \"context\": str(context or \"\").strip(),\n"
        "        \"instruction\": INSTRUCTION_TEXT,\n"
        "    }\n"
        "    return json.dumps(payload, ensure_ascii=True)\n"
    )


def _build_generated_skill_md(*, display_name: str, description: str, source_body: str) -> str:
    safe_description = description.strip() or "Generated from Claude Agent markdown"
    body = source_body.strip() or "No additional body content provided."
    return (
        f"# {display_name}\n\n"
        f"{safe_description}\n\n"
        "## Source Instructions\n\n"
        f"{body}\n"
    )


def _build_manifest(*, tool_name: str, description: str) -> dict[str, Any]:
    return {
        "name": tool_name,
        "package_version": "1.0.0",
        "description": (description.strip() or f"Generated dynamic skill: {tool_name}"),
        "entrypoint": "tool.py",
        "function": tool_name,
        "schema": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": "User request for this specialist skill",
                },
                "context": {
                    "type": "string",
                    "description": "Optional business context and constraints",
                    "default": "",
                },
            },
            "required": ["request"],
        },
    }


def _attach_manifest_hashes(skill_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    hashes = {
        "skill.md": hashlib.sha256((skill_dir / "skill.md").read_bytes()).hexdigest(),
        "tool.py": hashlib.sha256((skill_dir / "tool.py").read_bytes()).hexdigest(),
    }
    updated = dict(manifest)
    updated["package_files_sha256"] = hashes
    return updated


def _extract_file_payload_from_tool_messages(messages: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not messages:
        return None

    for msg in reversed(messages):
        if msg.get("role") != "tool":
            continue

        payload = msg.get("file_payload")
        if not isinstance(payload, dict):
            continue

        payload_type = str(payload.get("type") or "").strip().lower()
        if payload_type not in {"image", "file"}:
            continue

        base64_data = payload.get("base64")
        path_data = payload.get("path")
        has_base64 = isinstance(base64_data, str) and bool(base64_data) and base64_data != "<omitted>"
        has_path = isinstance(path_data, str) and bool(path_data.strip())
        if not has_base64 and not has_path:
            continue

        normalized: dict[str, Any] = {
            "type": payload_type,
            "filename": str(payload.get("filename") or "generated.bin"),
            "mime_type": str(payload.get("mime_type") or "application/octet-stream"),
        }
        if has_base64:
            normalized["base64"] = base64_data
        if has_path:
            normalized["path"] = path_data
        for key in (
            "model_id",
            "source_image",
            "width",
            "height",
            "num_frames",
            "num_inference_steps",
            "guidance_scale",
            "fps",
            "seed",
            "dtype",
            "size_bytes",
        ):
            if key in payload:
                normalized[key] = payload[key]
        return normalized

    return None


class RegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=256)
    full_name: str = Field(min_length=1, max_length=200)
    title: str = Field(default="", max_length=200)
    profile_bio: str = Field(default="", max_length=2000)


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: str


class TelegramLinkRequest(BaseModel):
    telegram_id: int


class CreateOrgRequest(BaseModel):
    org_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)


class SetRoleRequest(BaseModel):
    org_id: str = Field(min_length=1, max_length=128)
    role: Role


class SkillAssignmentRequest(BaseModel):
    org_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=128)


class AdminUserSummary(BaseModel):
    user_id: int
    email: str
    full_name: str
    title: str
    profile_bio: str = ""
    role: str
    in_team: bool
    force_password_change: bool = False


class GlobalAdminUserSummary(BaseModel):
    user_id: int
    email: str
    full_name: str
    title: str
    profile_bio: str = ""
    organization_ids: list[str]


class MembershipUpsertRequest(BaseModel):
    org_id: str = Field(min_length=1, max_length=128)
    role: Role = "member"


class CreateByobBotRequest(BaseModel):
    provider: str = Field(default="telegram", min_length=2, max_length=32)
    bot_name: str = Field(min_length=1, max_length=200)
    bot_token: str = Field(min_length=10, max_length=512)
    external_bot_id: str | None = Field(default=None, max_length=128)


class UpdateByobBotStatusRequest(BaseModel):
    is_active: bool


class ByobBotSummary(BaseModel):
    id: int
    org_id: str
    provider: str
    bot_name: str
    external_bot_id: str | None = None
    token_hint: str
    is_active: bool
    created_at: str
    updated_at: str


class OrgCreditBalanceResponse(BaseModel):
    org_id: str
    balance: int


class CreditAdjustmentRequest(BaseModel):
    amount: int = Field(gt=0, le=1_000_000_000)
    reason: str = Field(min_length=1, max_length=256)
    reference_type: str = Field(default="manual", max_length=64)
    reference_id: str = Field(default="", max_length=128)


class CreditLedgerItem(BaseModel):
    id: int
    delta: int
    balance_after: int
    reason: str
    actor_user_id: int
    reference_type: str
    reference_id: str
    created_at: str


class CreditLedgerResponse(BaseModel):
    org_id: str
    items: list[CreditLedgerItem]


class UpdateCreditPolicyRequest(BaseModel):
    daily_limit: int | None = Field(default=None, ge=0, le=1_000_000_000)
    monthly_limit: int | None = Field(default=None, ge=0, le=10_000_000_000)
    low_balance_threshold: int = Field(default=0, ge=0, le=1_000_000_000)


class CreditPolicyResponse(BaseModel):
    org_id: str
    daily_limit: int | None
    monthly_limit: int | None
    low_balance_threshold: int


class TelegramWebhookUpdate(BaseModel):
    update_id: int | None = None
    message: dict[str, Any] | None = None


class ByobQueueHealthResponse(BaseModel):
    org_id: str
    pending: int
    retry: int
    failed: int
    sent_last_24h: int
    oldest_due_at: str | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


class ChatMessage(BaseModel):
    sender_type: str
    sender_user_id: int | None
    content: str
    created_at: str


class ChatResponse(BaseModel):
    answer: str
    messages: list[ChatMessage]


class WsSendMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    client_message_id: str | None = Field(default=None, max_length=128)


class OrganizationSummary(BaseModel):
    org_id: str
    name: str


class AdminCreateUserRequest(BaseModel):
    org_id: str = Field(min_length=1, max_length=128)
    email: str
    password: str | None = Field(default=None, min_length=8, max_length=256)
    full_name: str = Field(min_length=1, max_length=200)
    title: str = Field(default="", max_length=200)
    profile_bio: str = Field(default="", max_length=2000)
    role: Role = "member"


class AdminUpdateUserRequest(BaseModel):
    org_id: str = Field(min_length=1, max_length=128)
    email: str
    full_name: str = Field(min_length=1, max_length=200)
    title: str = Field(default="", max_length=200)
    profile_bio: str = Field(default="", max_length=2000)
    role: Role = "member"
    force_password_change: bool | None = None


class ResetPasswordRequest(BaseModel):
    org_id: str = Field(min_length=1, max_length=128)
    ttl_minutes: int = Field(default=60, ge=5, le=1440)


class ForcePasswordChangeRequest(BaseModel):
    org_id: str = Field(min_length=1, max_length=128)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class ClaudeSkillConvertRequest(BaseModel):
    markdown: str = Field(min_length=1)
    skill_name: str | None = Field(default=None, max_length=128)
    overwrite: bool = False


class DynamicSkillDeleteResponse(BaseModel):
    status: str
    deleted: bool


class DynamicSkillSummary(BaseModel):
    folder: str
    tool_name: str
    description: str


class DynamicSkillListResponse(BaseModel):
    skills: list[DynamicSkillSummary]


class BulkClaudeConvertItem(BaseModel):
    filename: str
    status: str
    skill_name: str | None = None
    error: str | None = None


class BulkClaudeConvertResponse(BaseModel):
    created: int
    failed: int
    results: list[BulkClaudeConvertItem]


class BulkClaudeDryRunItem(BaseModel):
    filename: str
    status: str
    proposed_skill_name: str | None = None
    exists: bool = False
    error: str | None = None


class BulkClaudeDryRunResponse(BaseModel):
    total: int
    valid: int
    invalid: int
    results: list[BulkClaudeDryRunItem]


class ApiService:
    def __init__(self, settings: Settings) -> None:
        db_url = _resolve_db_url(settings.long_term_memory_database_url)
        if not db_url:
            raise RuntimeError("REMINDER_DATABASE_URL or DATABASE_URL is required for API")
        self.engine: Engine = create_engine(db_url, future=True, pool_pre_ping=True)
        self.settings = settings
        self.agent = OllamaLangGraphAgent(settings)
        self.rbac = RbacStore(db_url)
        self.long_term = LongTermMemoryStore.from_settings(settings)
        self.settings.dynamic_skills_dir.mkdir(parents=True, exist_ok=True)

    def __getattr__(self, name: str) -> Any:
        # Temporary compatibility shim: several ApiService methods are currently
        # defined on RealtimeChatHub with the same expected self attributes.
        fallback = getattr(RealtimeChatHub, name, None)
        if callable(fallback):
            return fallback.__get__(self, type(self))
        raise AttributeError(f"{type(self).__name__!s} object has no attribute {name!r}")

    def get_user_from_token(self, token: str) -> dict[str, Any]:
        token_hash = _hash_token(token)
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT u.user_id, u.email, u.full_name, u.title, u.profile_bio, u.telegram_id, t.expires_at
                    FROM auth_tokens t
                    JOIN auth_users u ON u.user_id = t.user_id
                    WHERE t.token_hash = :token_hash
                    LIMIT 1
                    """
                ),
                {"token_hash": token_hash},
            ).mappings().first()
        if row is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        expires_at = row.get("expires_at")
        if isinstance(expires_at, datetime):
            if expires_at.astimezone(UTC) <= datetime.now(UTC):
                raise HTTPException(status_code=401, detail="Token expired")
        return dict(row)


class RealtimeChatHub:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = asyncio.Lock()
        self._scope_sockets: dict[str, set[WebSocket]] = {}
        self._socket_scope: dict[WebSocket, str] = {}
        self._redis: redis_async.Redis | None = None
        self._pubsub_task: asyncio.Task[Any] | None = None
        self._broadcast_channel = f"{settings.redis_key_prefix}:ws:broadcast"

    @staticmethod
    def _scope_key(user_id: int) -> str:
        return str(max(0, int(user_id)))

    async def startup(self) -> None:
        if not self.settings.redis_url:
            return
        self._redis = redis_async.from_url(self.settings.redis_url, decode_responses=True)
        self._pubsub_task = asyncio.create_task(self._redis_listener(), name="chat-ws-redis-listener")

    async def shutdown(self) -> None:
        if self._pubsub_task is not None:
            self._pubsub_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pubsub_task
            self._pubsub_task = None
        if self._redis is not None:
            await self._redis.close()
            self._redis = None

    async def register_scope(self, websocket: WebSocket, user_id: int) -> None:
        scope_key = self._scope_key(user_id)
        async with self._lock:
            previous = self._socket_scope.get(websocket)
            if previous and previous in self._scope_sockets:
                self._scope_sockets[previous].discard(websocket)
                if not self._scope_sockets[previous]:
                    del self._scope_sockets[previous]
            self._socket_scope[websocket] = scope_key
            self._scope_sockets.setdefault(scope_key, set()).add(websocket)

    async def unregister(self, websocket: WebSocket) -> None:
        async with self._lock:
            scope_key = self._socket_scope.pop(websocket, None)
            if scope_key and scope_key in self._scope_sockets:
                self._scope_sockets[scope_key].discard(websocket)
                if not self._scope_sockets[scope_key]:
                    del self._scope_sockets[scope_key]

    async def publish_snapshot(self, snapshot_payload: dict[str, Any]) -> None:
        if self._redis is not None:
            await self._redis.publish(self._broadcast_channel, json.dumps(snapshot_payload, ensure_ascii=True))
        await self._broadcast_local(snapshot_payload)

    async def _broadcast_local(self, snapshot_payload: dict[str, Any]) -> None:
        raw_user_id = snapshot_payload.get("user_id")
        if raw_user_id is None:
            return
        try:
            user_id = int(raw_user_id)
        except Exception:
            return
        scope_key = self._scope_key(user_id)
        async with self._lock:
            sockets = list(self._scope_sockets.get(scope_key, set()))

        dropped: list[WebSocket] = []
        for socket in sockets:
            try:
                await socket.send_json(snapshot_payload)
            except Exception:
                dropped.append(socket)

        if dropped:
            async with self._lock:
                for socket in dropped:
                    old_key = self._socket_scope.pop(socket, None)
                    if old_key and old_key in self._scope_sockets:
                        self._scope_sockets[old_key].discard(socket)
                        if not self._scope_sockets[old_key]:
                            del self._scope_sockets[old_key]

    async def _redis_listener(self) -> None:
        if self._redis is None:
            return
        pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(self._broadcast_channel)
        try:
            while True:
                message = await pubsub.get_message(timeout=1.0)
                if not message:
                    await asyncio.sleep(0.01)
                    continue
                raw = message.get("data")
                if not raw:
                    continue
                try:
                    payload = json.loads(str(raw))
                except json.JSONDecodeError:
                    continue
                await self._broadcast_local(payload)
        finally:
            await pubsub.unsubscribe(self._broadcast_channel)
            await pubsub.close()


    def _supports_auth_otp_fields(self) -> bool:
        try:
            with self.engine.begin() as conn:
                row = conn.execute(
                    text(
                        """
                        SELECT COUNT(*) AS cnt
                        FROM information_schema.columns
                        WHERE table_name = 'auth_users'
                          AND column_name IN (
                            'force_password_change',
                            'one_time_password_hash',
                            'one_time_password_expires_at'
                          )
                        """
                    )
                ).mappings().first()
            return int(row.get("cnt") or 0) >= 3 if row else False
        except Exception:
            return False

    def register_user(self, payload: RegisterRequest) -> int:
        password_hash = _hash_password(payload.password)
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            existing_admin = conn.execute(
                text(
                    """
                    SELECT 1
                    FROM app_users au
                    JOIN auth_users u ON u.user_id = au.user_id
                    WHERE au.org_id = :org_id AND au.role = 'admin'
                    LIMIT 1
                    """
                ),
                {"org_id": USER_SCOPE_ORG_ID},
            ).first()
            existing = conn.execute(
                text("SELECT user_id FROM auth_users WHERE lower(email) = lower(:email) LIMIT 1"),
                {"email": payload.email.strip()},
            ).mappings().first()
            if existing is not None:
                raise HTTPException(status_code=409, detail="Email already registered")

            row = conn.execute(
                text(
                    """
                    INSERT INTO auth_users (email, password_hash, full_name, title, profile_bio, created_at, updated_at)
                    VALUES (:email, :password_hash, :full_name, :title, :profile_bio, :created_at, :updated_at)
                    RETURNING user_id
                    """
                ),
                {
                    "email": payload.email.strip(),
                    "password_hash": password_hash,
                    "full_name": payload.full_name.strip(),
                    "title": payload.title.strip(),
                    "profile_bio": payload.profile_bio.strip(),
                    "created_at": now,
                    "updated_at": now,
                },
            ).mappings().first()

            user_id = int(row["user_id"])
            self.rbac.ensure_organization(org_id=USER_SCOPE_ORG_ID, name=USER_SCOPE_ORG_ID)
            role: Role = "admin" if existing_admin is None else "member"
            self.rbac.upsert_user(org_id=USER_SCOPE_ORG_ID, user_id=user_id, role=role)
            conn.execute(
                text(
                    """
                    INSERT INTO org_memberships (org_id, user_id, created_at)
                    VALUES (:org_id, :user_id, :created_at)
                    ON CONFLICT (org_id, user_id) DO NOTHING
                    """
                ),
                {
                    "org_id": USER_SCOPE_ORG_ID,
                    "user_id": user_id,
                    "created_at": now,
                },
            )
        return user_id

    def create_token(self, *, email: str, password: str) -> TokenResponse:
        supports_otp = self._supports_auth_otp_fields()
        with self.engine.begin() as conn:
            if supports_otp:
                row = conn.execute(
                    text(
                        """
                        SELECT
                            user_id,
                            password_hash,
                            one_time_password_hash,
                            one_time_password_expires_at,
                            force_password_change
                        FROM auth_users
                        WHERE lower(email) = lower(:email)
                        LIMIT 1
                        """
                    ),
                    {"email": email.strip()},
                ).mappings().first()
            else:
                row = conn.execute(
                    text(
                        """
                        SELECT user_id, password_hash
                        FROM auth_users
                        WHERE lower(email) = lower(:email)
                        LIMIT 1
                        """
                    ),
                    {"email": email.strip()},
                ).mappings().first()

            if row is None:
                raise HTTPException(status_code=401, detail="Invalid credentials")

            uses_otp = False
            base_ok = _verify_password(password, str(row.get("password_hash") or ""))
            if base_ok:
                authenticated = True
            else:
                otp_hash = str(row.get("one_time_password_hash") or "")
                otp_expires_at = row.get("one_time_password_expires_at")
                otp_not_expired = isinstance(otp_expires_at, datetime) and otp_expires_at.astimezone(UTC) > datetime.now(UTC)
                authenticated = bool(otp_hash) and otp_not_expired and _verify_password(password, otp_hash)
                uses_otp = authenticated

            if not authenticated:
                raise HTTPException(status_code=401, detail="Invalid credentials")

            raw_token = secrets.token_urlsafe(48)
            token_hash = _hash_token(raw_token)
            now = datetime.now(UTC)
            expires_at = now + timedelta(hours=TOKEN_TTL_HOURS)
            conn.execute(
                text(
                    """
                    INSERT INTO auth_tokens (token_hash, user_id, expires_at, created_at)
                    VALUES (:token_hash, :user_id, :expires_at, :created_at)
                    """
                ),
                {
                    "token_hash": token_hash,
                    "user_id": int(row["user_id"]),
                    "expires_at": expires_at,
                    "created_at": now,
                },
            )
            if uses_otp and supports_otp:
                conn.execute(
                    text(
                        """
                        UPDATE auth_users
                        SET force_password_change = TRUE,
                            one_time_password_hash = NULL,
                            one_time_password_expires_at = NULL,
                            updated_at = :updated_at
                        WHERE user_id = :user_id
                        """
                    ),
                    {
                        "user_id": int(row["user_id"]),
                        "updated_at": now,
                    },
                )
        return TokenResponse(access_token=raw_token, expires_at=expires_at.isoformat())

    def get_user_from_token(self, token: str) -> dict[str, Any]:
        token_hash = _hash_token(token)
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT u.user_id, u.email, u.full_name, u.title, u.profile_bio, u.telegram_id, t.expires_at
                    FROM auth_tokens t
                    JOIN auth_users u ON u.user_id = t.user_id
                    WHERE t.token_hash = :token_hash
                    LIMIT 1
                    """
                ),
                {"token_hash": token_hash},
            ).mappings().first()
        if row is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        expires_at = row.get("expires_at")
        if isinstance(expires_at, datetime):
            if expires_at.astimezone(UTC) <= datetime.now(UTC):
                raise HTTPException(status_code=401, detail="Token expired")
        return dict(row)

    def change_password(self, *, user_id: int, current_password: str, new_password: str) -> None:
        supports_otp = self._supports_auth_otp_fields()
        with self.engine.begin() as conn:
            if supports_otp:
                row = conn.execute(
                    text(
                        """
                        SELECT password_hash, one_time_password_hash, one_time_password_expires_at
                        FROM auth_users
                        WHERE user_id = :user_id
                        LIMIT 1
                        """
                    ),
                    {"user_id": int(user_id)},
                ).mappings().first()
            else:
                row = conn.execute(
                    text(
                        """
                        SELECT password_hash
                        FROM auth_users
                        WHERE user_id = :user_id
                        LIMIT 1
                        """
                    ),
                    {"user_id": int(user_id)},
                ).mappings().first()
            if row is None:
                raise HTTPException(status_code=404, detail="User not found")

            base_ok = _verify_password(current_password, str(row.get("password_hash") or ""))
            otp_hash = str(row.get("one_time_password_hash") or "")
            otp_expires_at = row.get("one_time_password_expires_at")
            otp_ok = bool(otp_hash) and isinstance(otp_expires_at, datetime) and otp_expires_at.astimezone(UTC) > datetime.now(UTC)
            otp_ok = otp_ok and _verify_password(current_password, otp_hash)

            if not base_ok and not otp_ok:
                raise HTTPException(status_code=401, detail="Current password is invalid")

            if supports_otp:
                conn.execute(
                    text(
                        """
                        UPDATE auth_users
                        SET password_hash = :password_hash,
                            force_password_change = FALSE,
                            one_time_password_hash = NULL,
                            one_time_password_expires_at = NULL,
                            updated_at = :updated_at
                        WHERE user_id = :user_id
                        """
                    ),
                    {
                        "password_hash": _hash_password(new_password),
                        "updated_at": datetime.now(UTC),
                        "user_id": int(user_id),
                    },
                )
            else:
                conn.execute(
                    text(
                        """
                        UPDATE auth_users
                        SET password_hash = :password_hash,
                            updated_at = :updated_at
                        WHERE user_id = :user_id
                        """
                    ),
                    {
                        "password_hash": _hash_password(new_password),
                        "updated_at": datetime.now(UTC),
                        "user_id": int(user_id),
                    },
                )

    def link_telegram(self, *, user_id: int, telegram_id: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE auth_users
                    SET telegram_id = :telegram_id, updated_at = :updated_at
                    WHERE user_id = :user_id
                    """
                ),
                {
                    "telegram_id": int(telegram_id),
                    "updated_at": datetime.now(UTC),
                    "user_id": int(user_id),
                },
            )

    def list_organizations(self, *, actor_user_id: int) -> list[OrganizationSummary]:
        self._enforce_admin(actor_user_id)
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT org_id, name
                    FROM organizations
                    ORDER BY org_id ASC
                    """
                )
            ).mappings().all()
        return [
            OrganizationSummary(org_id=str(row.get("org_id") or ""), name=str(row.get("name") or row.get("org_id") or ""))
            for row in rows
            if str(row.get("org_id") or "").strip()
        ]

    def create_user_by_admin(self, *, actor_user_id: int, payload: AdminCreateUserRequest) -> dict[str, Any]:
        self._enforce_admin(actor_user_id)
        now = datetime.now(UTC)
        generated_password = payload.password or secrets.token_urlsafe(12)
        supports_otp = self._supports_auth_otp_fields()
        with self.engine.begin() as conn:
            existing = conn.execute(
                text("SELECT user_id FROM auth_users WHERE lower(email) = lower(:email) LIMIT 1"),
                {"email": payload.email.strip()},
            ).mappings().first()
            if existing is not None:
                raise HTTPException(status_code=409, detail="Email already registered")

            if supports_otp:
                row = conn.execute(
                    text(
                        """
                        INSERT INTO auth_users (
                            email,
                            password_hash,
                            full_name,
                            title,
                            profile_bio,
                            force_password_change,
                            created_at,
                            updated_at
                        )
                        VALUES (
                            :email,
                            :password_hash,
                            :full_name,
                            :title,
                            :profile_bio,
                            TRUE,
                            :created_at,
                            :updated_at
                        )
                        RETURNING user_id
                        """
                    ),
                    {
                        "email": payload.email.strip(),
                        "password_hash": _hash_password(generated_password),
                        "full_name": payload.full_name.strip(),
                        "title": payload.title.strip(),
                        "profile_bio": payload.profile_bio.strip(),
                        "created_at": now,
                        "updated_at": now,
                    },
                ).mappings().first()
            else:
                row = conn.execute(
                    text(
                        """
                        INSERT INTO auth_users (
                            email,
                            password_hash,
                            full_name,
                            title,
                            profile_bio,
                            created_at,
                            updated_at
                        )
                        VALUES (
                            :email,
                            :password_hash,
                            :full_name,
                            :title,
                            :profile_bio,
                            :created_at,
                            :updated_at
                        )
                        RETURNING user_id
                        """
                    ),
                    {
                        "email": payload.email.strip(),
                        "password_hash": _hash_password(generated_password),
                        "full_name": payload.full_name.strip(),
                        "title": payload.title.strip(),
                        "profile_bio": payload.profile_bio.strip(),
                        "created_at": now,
                        "updated_at": now,
                    },
                ).mappings().first()

            user_id = int(row["user_id"])
            conn.execute(
                text(
                    """
                    INSERT INTO org_memberships (org_id, user_id, created_at)
                    VALUES (:org_id, :user_id, :created_at)
                    ON CONFLICT (org_id, user_id) DO NOTHING
                    """
                ),
                {
                    "org_id": payload.org_id,
                    "user_id": user_id,
                    "created_at": now,
                },
            )

        self.rbac.upsert_user(org_id=payload.org_id, user_id=user_id, role=payload.role)
        self.rbac.audit(
            org_id=payload.org_id,
            team_id="",
            actor_user_id=actor_user_id,
            action="rbac.create_user",
            target_type="user",
            target_id=str(user_id),
            details={"role": payload.role, "email": payload.email.strip()},
        )
        return {
            "user_id": user_id,
            "email": payload.email.strip(),
            "one_time_password": generated_password,
        }

    def update_user_by_admin(self, *, actor_user_id: int, target_user_id: int, payload: AdminUpdateUserRequest) -> None:
        self._enforce_admin(actor_user_id)
        supports_otp = self._supports_auth_otp_fields()
        with self.engine.begin() as conn:
            if supports_otp and payload.force_password_change is not None:
                conn.execute(
                    text(
                        """
                        UPDATE auth_users
                        SET email = :email,
                            full_name = :full_name,
                            title = :title,
                            profile_bio = :profile_bio,
                            force_password_change = :force_password_change,
                            updated_at = :updated_at
                        WHERE user_id = :user_id
                        """
                    ),
                    {
                        "email": payload.email.strip(),
                        "full_name": payload.full_name.strip(),
                        "title": payload.title.strip(),
                        "profile_bio": payload.profile_bio.strip(),
                        "force_password_change": bool(payload.force_password_change),
                        "updated_at": datetime.now(UTC),
                        "user_id": int(target_user_id),
                    },
                )
            else:
                conn.execute(
                    text(
                        """
                        UPDATE auth_users
                        SET email = :email,
                            full_name = :full_name,
                            title = :title,
                            profile_bio = :profile_bio,
                            updated_at = :updated_at
                        WHERE user_id = :user_id
                        """
                    ),
                    {
                        "email": payload.email.strip(),
                        "full_name": payload.full_name.strip(),
                        "title": payload.title.strip(),
                        "profile_bio": payload.profile_bio.strip(),
                        "updated_at": datetime.now(UTC),
                        "user_id": int(target_user_id),
                    },
                )

        self.rbac.upsert_user(org_id=payload.org_id, user_id=target_user_id, role=payload.role)

    def reset_user_password_one_time(
        self,
        *,
        actor_user_id: int,
        org_id: str,
        target_user_id: int,
        ttl_minutes: int,
    ) -> dict[str, Any]:
        self._enforce_admin(actor_user_id)
        if not self._supports_auth_otp_fields():
            raise HTTPException(status_code=400, detail="OTP fields are not migrated. Run alembic upgrade head.")
        otp = secrets.token_urlsafe(12)
        expires_at = datetime.now(UTC) + timedelta(minutes=max(5, int(ttl_minutes)))

        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE auth_users
                    SET one_time_password_hash = :otp_hash,
                        one_time_password_expires_at = :otp_expires_at,
                        force_password_change = TRUE,
                        updated_at = :updated_at
                    WHERE user_id = :user_id
                    """
                ),
                {
                    "otp_hash": _hash_password(otp),
                    "otp_expires_at": expires_at,
                    "updated_at": datetime.now(UTC),
                    "user_id": int(target_user_id),
                },
            )

        self.rbac.audit(
            org_id=org_id,
            team_id="",
            actor_user_id=actor_user_id,
            action="rbac.reset_user_otp",
            target_type="user",
            target_id=str(target_user_id),
            details={"ttl_minutes": int(ttl_minutes)},
        )
        return {
            "user_id": int(target_user_id),
            "one_time_password": otp,
            "expires_at": expires_at.isoformat(),
        }

    def force_user_password_change(self, *, actor_user_id: int, org_id: str, target_user_id: int) -> dict[str, Any]:
        self._enforce_admin(actor_user_id)
        if not self._supports_auth_otp_fields():
            raise HTTPException(status_code=400, detail="OTP fields are not migrated. Run alembic upgrade head.")

        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE auth_users
                    SET force_password_change = TRUE,
                        one_time_password_hash = NULL,
                        one_time_password_expires_at = NULL,
                        updated_at = :updated_at
                    WHERE user_id = :user_id
                    """
                ),
                {
                    "updated_at": datetime.now(UTC),
                    "user_id": int(target_user_id),
                },
            )

        self.rbac.audit(
            org_id=org_id,
            team_id="",
            actor_user_id=actor_user_id,
            action="rbac.force_user_password_change",
            target_type="user",
            target_id=str(target_user_id),
            details={},
        )
        return {"status": "ok", "user_id": int(target_user_id), "force_password_change": True}

    def _is_global_admin(self, user_id: int) -> bool:
        if int(user_id) in self.settings.telegram_admin_user_ids:
            return True
        role = self.rbac.get_role(
            org_id=USER_SCOPE_ORG_ID,
            user_id=int(user_id),
            fallback_role="member",
        )
        return role in {"owner", "admin"}

    def _enforce_admin(self, user_id: int) -> None:
        if not self._is_global_admin(user_id):
            raise HTTPException(status_code=403, detail="Admin access required")

    def _enforce_org_admin(self, *, actor_user_id: int, org_id: str) -> None:
        if self._is_global_admin(actor_user_id):
            return
        role = self.rbac.get_role(
            org_id=org_id,
            user_id=int(actor_user_id),
            fallback_role="member",
        )
        if role not in {"owner", "admin"}:
            raise HTTPException(status_code=403, detail="Organization admin access required")

    def create_org(self, *, actor_user_id: int, payload: CreateOrgRequest) -> None:
        # Global admins can create organizations centrally. Regular users can self-serve
        # and become admins of their own organization.
        self.rbac.create_organization(actor_user_id=actor_user_id, org_id=payload.org_id, name=payload.name)
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO org_memberships (org_id, user_id, created_at)
                    VALUES (:org_id, :user_id, :created_at)
                    ON CONFLICT (org_id, user_id) DO NOTHING
                    """
                ),
                {
                    "org_id": payload.org_id,
                    "user_id": int(actor_user_id),
                    "created_at": now,
                },
            )
        self.rbac.upsert_user(org_id=payload.org_id, user_id=int(actor_user_id), role="owner")

    @staticmethod
    def _normalize_provider(value: str) -> str:
        provider = (value or "").strip().lower()
        if provider != "telegram":
            raise HTTPException(status_code=400, detail="Only telegram provider is currently supported")
        return provider

    @staticmethod
    def _derive_keystream(*, key: bytes, nonce: bytes, length: int) -> bytes:
        output = bytearray()
        counter = 0
        while len(output) < length:
            block = hashlib.sha256(key + nonce + counter.to_bytes(4, "big")).digest()
            output.extend(block)
            counter += 1
        return bytes(output[:length])

    def _encode_bot_token(self, token: str) -> str:
        key_text = self.settings.byob_token_crypto_key
        if not key_text:
            return base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii")

        key = hashlib.sha256(key_text.encode("utf-8")).digest()
        plaintext = token.encode("utf-8")
        nonce = secrets.token_bytes(16)
        keystream = self._derive_keystream(key=key, nonce=nonce, length=len(plaintext))
        ciphertext = bytes([a ^ b for a, b in zip(plaintext, keystream)])
        mac = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()[:16]
        return base64.urlsafe_b64encode(nonce + mac + ciphertext).decode("ascii")

    def _decode_bot_token(self, ciphertext: str | None) -> str:
        if not ciphertext:
            return ""
        try:
            raw = base64.urlsafe_b64decode(ciphertext.encode("ascii"))
            key_text = self.settings.byob_token_crypto_key
            if not key_text:
                return raw.decode("utf-8")

            if len(raw) < 33:
                return ""
            nonce = raw[:16]
            mac = raw[16:32]
            payload = raw[32:]
            key = hashlib.sha256(key_text.encode("utf-8")).digest()
            expected = hmac.new(key, nonce + payload, hashlib.sha256).digest()[:16]
            if not hmac.compare_digest(mac, expected):
                return ""

            keystream = self._derive_keystream(key=key, nonce=nonce, length=len(payload))
            plaintext = bytes([a ^ b for a, b in zip(payload, keystream)])
            return plaintext.decode("utf-8")
        except Exception:
            return ""

    @staticmethod
    def _retry_backoff_seconds(*, base: int, attempt: int) -> int:
        safe_base = max(1, int(base))
        safe_attempt = max(0, int(attempt))
        return safe_base * (2 ** min(safe_attempt, 8))

    def _build_telegram_webhook_url(self, *, org_id: str, bot_id: int) -> str | None:
        base = (self.settings.byob_public_base_url or "").strip().rstrip("/")
        if not base:
            return None
        return f"{base}/api/v1/byob/telegram/{org_id}/{int(bot_id)}/webhook"

    def _call_telegram_api(self, *, token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        base = (self.settings.telegram_api_base_url or "https://api.telegram.org").strip().rstrip("/")
        method_name = method.strip().lstrip("/")
        encoded_payload = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        req = urlrequest.Request(
            url=f"{base}/bot{token}/{method_name}",
            data=encoded_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlrequest.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urlerror.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp is not None else str(exc)
            raise HTTPException(status_code=502, detail=f"Telegram API error: {detail}") from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Telegram API call failed: {exc}") from exc

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=502, detail="Telegram API returned invalid JSON") from exc
        if not bool(parsed.get("ok")):
            raise HTTPException(status_code=502, detail=f"Telegram API returned non-ok response: {parsed}")
        return parsed

    def _vault_write_bot_token(self, *, org_id: str, bot_connection_id: int, token: str) -> str | None:
        vault_addr = (self.settings.byob_vault_addr or "").strip().rstrip("/")
        vault_token = (self.settings.byob_vault_token or "").strip()
        if not vault_addr or not vault_token:
            return None

        logical_path = f"smartai/byob/{org_id}/{int(bot_connection_id)}"
        mount = (self.settings.byob_vault_mount or "secret").strip().strip("/")
        encoded_path = urlparse.quote(logical_path, safe="/")
        url = f"{vault_addr}/v1/{mount}/data/{encoded_path}"
        payload = {"data": {"bot_token": token}}
        req = urlrequest.Request(
            url=url,
            data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
            headers={
                "X-Vault-Token": vault_token,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlrequest.urlopen(req, timeout=10):
                pass
            return f"{mount}/{logical_path}"
        except Exception:
            return None

    def _vault_read_bot_token(self, *, vault_ref: str | None) -> str:
        if not vault_ref:
            return ""
        vault_addr = (self.settings.byob_vault_addr or "").strip().rstrip("/")
        vault_token = (self.settings.byob_vault_token or "").strip()
        if not vault_addr or not vault_token:
            return ""

        parts = str(vault_ref).strip().split("/", 1)
        if len(parts) != 2:
            return ""
        mount, logical = parts[0].strip(), parts[1].strip()
        if not mount or not logical:
            return ""

        encoded_path = urlparse.quote(logical, safe="/")
        url = f"{vault_addr}/v1/{mount}/data/{encoded_path}"
        req = urlrequest.Request(
            url=url,
            headers={"X-Vault-Token": vault_token},
            method="GET",
        )
        try:
            with urlrequest.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(body)
            token = (((parsed or {}).get("data") or {}).get("data") or {}).get("bot_token")
            return str(token or "")
        except Exception:
            return ""

    def _load_bot_token_for_connection(self, *, org_id: str, bot_connection_id: int) -> str:
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT token_vault_path, token_ciphertext
                    FROM byob_bot_connections
                    WHERE id = :bot_id AND org_id = :org_id
                    LIMIT 1
                    """
                ),
                {
                    "bot_id": int(bot_connection_id),
                    "org_id": org_id,
                },
            ).mappings().first()
        if row is None:
            return ""

        token = self._vault_read_bot_token(vault_ref=str(row.get("token_vault_path") or ""))
        if token:
            return token
        return self._decode_bot_token(str(row.get("token_ciphertext") or ""))

    def _enforce_telegram_ip(self, *, client_ip: str | None) -> None:
        if not self.settings.byob_enforce_telegram_ip:
            return
        raw_ip = (client_ip or "").strip()
        if not raw_ip:
            raise HTTPException(status_code=401, detail="Missing client IP")
        try:
            parsed_ip = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail="Invalid client IP") from exc

        for cidr in self.settings.byob_telegram_ip_allowlist:
            try:
                if parsed_ip in ipaddress.ip_network(cidr, strict=False):
                    return
            except ValueError:
                continue
        raise HTTPException(status_code=401, detail="Client IP is not in Telegram allowlist")

    def _consume_rate_limit(self, *, scope: str, scope_key: str, limit_per_minute: int) -> None:
        if limit_per_minute <= 0:
            return
        now = datetime.now(UTC)
        window = now.replace(second=0, microsecond=0)
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    INSERT INTO byob_rate_limit_counters (scope, scope_key, window_start, request_count, updated_at)
                    VALUES (:scope, :scope_key, :window_start, 1, :updated_at)
                    ON CONFLICT (scope, scope_key, window_start)
                    DO UPDATE SET request_count = byob_rate_limit_counters.request_count + 1,
                                  updated_at = EXCLUDED.updated_at
                    RETURNING request_count
                    """
                ),
                {
                    "scope": scope,
                    "scope_key": scope_key,
                    "window_start": window,
                    "updated_at": now,
                },
            ).mappings().first()

        used = int((row or {}).get("request_count") or 0)
        if used > int(limit_per_minute):
            raise HTTPException(status_code=429, detail=f"Rate limit exceeded for scope {scope}")

    def _set_telegram_webhook(self, *, token: str, org_id: str, bot_id: int, webhook_secret: str) -> None:
        webhook_url = self._build_telegram_webhook_url(org_id=org_id, bot_id=bot_id)
        if not webhook_url:
            return
        self._call_telegram_api(
            token=token,
            method="setWebhook",
            payload={
                "url": webhook_url,
                "secret_token": webhook_secret,
                "drop_pending_updates": False,
                "allowed_updates": ["message"],
            },
        )

    def _queue_byob_outbound_message(
        self,
        *,
        org_id: str,
        bot_connection_id: int,
        chat_id: int,
        payload: dict[str, Any],
    ) -> int:
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    INSERT INTO byob_outbound_queue (
                        org_id,
                        bot_connection_id,
                        chat_id,
                        payload_json,
                        status,
                        attempt_count,
                        next_retry_at,
                        last_error,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        :org_id,
                        :bot_connection_id,
                        :chat_id,
                        :payload_json,
                        'pending',
                        0,
                        :next_retry_at,
                        '',
                        :created_at,
                        :updated_at
                    )
                    RETURNING id
                    """
                ),
                {
                    "org_id": org_id,
                    "bot_connection_id": int(bot_connection_id),
                    "chat_id": int(chat_id),
                    "payload_json": json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
                    "next_retry_at": datetime.now(UTC),
                    "created_at": datetime.now(UTC),
                    "updated_at": datetime.now(UTC),
                },
            ).mappings().first()
        return int((row or {}).get("id") or 0)

    def _deliver_byob_outbound_queue(self, *, org_id: str, bot_connection_id: int) -> None:
        with self.engine.begin() as conn:
            bot_row = conn.execute(
                text(
                    """
                    SELECT token_ciphertext
                    FROM byob_bot_connections
                    WHERE id = :bot_id AND org_id = :org_id AND is_active = TRUE
                    LIMIT 1
                    """
                ),
                {
                    "bot_id": int(bot_connection_id),
                    "org_id": org_id,
                },
            ).mappings().first()

        token = self._load_bot_token_for_connection(org_id=org_id, bot_connection_id=bot_connection_id)
        if not token:
            return

        with self.engine.begin() as conn:
            queue_rows = conn.execute(
                text(
                    """
                    SELECT id, payload_json, attempt_count
                    FROM byob_outbound_queue
                    WHERE org_id = :org_id
                      AND bot_connection_id = :bot_connection_id
                      AND status IN ('pending', 'retry')
                      AND next_retry_at <= :now
                    ORDER BY created_at ASC
                    LIMIT :limit
                    """
                ),
                {
                    "org_id": org_id,
                    "bot_connection_id": int(bot_connection_id),
                    "now": datetime.now(UTC),
                    "limit": max(1, int(self.settings.byob_webhook_delivery_batch)),
                },
            ).mappings().all()

        for row in queue_rows:
            queue_id = int(row.get("id") or 0)
            attempts = int(row.get("attempt_count") or 0)
            raw_payload = str(row.get("payload_json") or "{}")
            try:
                payload = json.loads(raw_payload)
                self._call_telegram_api(token=token, method="sendMessage", payload=payload)
                with self.engine.begin() as conn:
                    conn.execute(
                        text(
                            """
                            UPDATE byob_outbound_queue
                            SET status = 'sent',
                                updated_at = :updated_at,
                                last_error = ''
                            WHERE id = :id
                            """
                        ),
                        {
                            "id": queue_id,
                            "updated_at": datetime.now(UTC),
                        },
                    )
            except Exception as exc:
                next_attempt = attempts + 1
                max_attempts = max(1, int(self.settings.byob_webhook_retry_max_attempts))
                terminal = next_attempt >= max_attempts
                next_status = "failed" if terminal else "retry"
                delay_seconds = self._retry_backoff_seconds(
                    base=int(self.settings.byob_webhook_retry_base_seconds),
                    attempt=next_attempt,
                )
                with self.engine.begin() as conn:
                    conn.execute(
                        text(
                            """
                            UPDATE byob_outbound_queue
                            SET status = :status,
                                attempt_count = :attempt_count,
                                next_retry_at = :next_retry_at,
                                last_error = :last_error,
                                updated_at = :updated_at
                            WHERE id = :id
                            """
                        ),
                        {
                            "status": next_status,
                            "attempt_count": next_attempt,
                            "next_retry_at": datetime.now(UTC) + timedelta(seconds=delay_seconds),
                            "last_error": str(exc)[:4000],
                            "updated_at": datetime.now(UTC),
                            "id": queue_id,
                        },
                    )

    def process_byob_delivery_backlog(self) -> int:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT DISTINCT org_id, bot_connection_id
                    FROM byob_outbound_queue
                    WHERE status IN ('pending', 'retry')
                      AND next_retry_at <= :now
                    ORDER BY org_id ASC, bot_connection_id ASC
                    LIMIT :limit
                    """
                ),
                {
                    "now": datetime.now(UTC),
                    "limit": 200,
                },
            ).mappings().all()

        processed = 0
        for row in rows:
            org_id = str(row.get("org_id") or "").strip()
            bot_connection_id = int(row.get("bot_connection_id") or 0)
            if not org_id or bot_connection_id <= 0:
                continue
            self._deliver_byob_outbound_queue(org_id=org_id, bot_connection_id=bot_connection_id)
            processed += 1
        return processed

    def get_byob_queue_health(self, *, actor_user_id: int, org_id: str) -> ByobQueueHealthResponse:
        normalized_org = org_id.strip()
        if not normalized_org:
            raise HTTPException(status_code=400, detail="Organization ID is required")
        self._enforce_org_admin(actor_user_id=actor_user_id, org_id=normalized_org)

        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT
                        SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                        SUM(CASE WHEN status = 'retry' THEN 1 ELSE 0 END) AS retry,
                        SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                        SUM(CASE WHEN status = 'sent' AND created_at >= (now() - interval '24 hours') THEN 1 ELSE 0 END) AS sent_last_24h,
                        MIN(CASE WHEN status IN ('pending', 'retry') THEN next_retry_at END)::text AS oldest_due_at
                    FROM byob_outbound_queue
                    WHERE org_id = :org_id
                    """
                ),
                {"org_id": normalized_org},
            ).mappings().first()

        return ByobQueueHealthResponse(
            org_id=normalized_org,
            pending=int((row or {}).get("pending") or 0),
            retry=int((row or {}).get("retry") or 0),
            failed=int((row or {}).get("failed") or 0),
            sent_last_24h=int((row or {}).get("sent_last_24h") or 0),
            oldest_due_at=(row or {}).get("oldest_due_at"),
        )

    def register_byob_bot(self, *, actor_user_id: int, org_id: str, payload: CreateByobBotRequest) -> ByobBotSummary:
        normalized_org = org_id.strip()
        if not normalized_org:
            raise HTTPException(status_code=400, detail="Organization ID is required")
        self._enforce_org_admin(actor_user_id=actor_user_id, org_id=normalized_org)

        provider = self._normalize_provider(payload.provider)
        bot_name = payload.bot_name.strip()
        token = payload.bot_token.strip()
        external_bot_id = (payload.external_bot_id or "").strip() or None
        if not token:
            raise HTTPException(status_code=400, detail="Bot token is required")

        token_hash = _hash_token(token)
        token_hint = f"***{token[-6:]}" if len(token) >= 6 else "***"
        now = datetime.now(UTC)

        with self.engine.begin() as conn:
            existing = conn.execute(
                text(
                    """
                    SELECT id
                    FROM byob_bot_connections
                    WHERE org_id = :org_id AND provider = :provider AND token_hash = :token_hash
                    LIMIT 1
                    """
                ),
                {
                    "org_id": normalized_org,
                    "provider": provider,
                    "token_hash": token_hash,
                },
            ).mappings().first()
            if existing is not None:
                raise HTTPException(status_code=409, detail="Bot token is already registered for this organization")

            row = conn.execute(
                text(
                    """
                    INSERT INTO byob_bot_connections (
                        org_id,
                        provider,
                        bot_name,
                        external_bot_id,
                        token_hash,
                        token_ciphertext,
                        token_vault_path,
                        token_hint,
                        webhook_secret,
                        is_active,
                        created_by,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        :org_id,
                        :provider,
                        :bot_name,
                        :external_bot_id,
                        :token_hash,
                        :token_ciphertext,
                        :token_vault_path,
                        :token_hint,
                        :webhook_secret,
                        TRUE,
                        :created_by,
                        :created_at,
                        :updated_at
                    )
                    RETURNING id, org_id, provider, bot_name, external_bot_id, token_hint, is_active,
                              created_at::text AS created_at, updated_at::text AS updated_at
                    """
                ),
                {
                    "org_id": normalized_org,
                    "provider": provider,
                    "bot_name": bot_name,
                    "external_bot_id": external_bot_id,
                    "token_hash": token_hash,
                    "token_ciphertext": self._encode_bot_token(token),
                    "token_vault_path": None,
                    "token_hint": token_hint,
                    "webhook_secret": secrets.token_urlsafe(24),
                    "created_by": int(actor_user_id),
                    "created_at": now,
                    "updated_at": now,
                },
            ).mappings().first()

        bot_id = int((row or {}).get("id") or 0)
        vault_ref = self._vault_write_bot_token(org_id=normalized_org, bot_connection_id=bot_id, token=token)
        if self.settings.byob_vault_required and not vault_ref:
            raise HTTPException(status_code=500, detail="Vault token storage is required but failed")
        if vault_ref:
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE byob_bot_connections
                        SET token_vault_path = :token_vault_path,
                            updated_at = :updated_at
                        WHERE id = :id
                        """
                    ),
                    {
                        "token_vault_path": vault_ref,
                        "updated_at": datetime.now(UTC),
                        "id": bot_id,
                    },
                )

        try:
            self._set_telegram_webhook(
                token=token,
                org_id=normalized_org,
                bot_id=bot_id,
                webhook_secret=str((row or {}).get("webhook_secret") or ""),
            )
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE byob_bot_connections
                        SET last_verified_at = :last_verified_at,
                            updated_at = :updated_at
                        WHERE id = :id
                        """
                    ),
                    {
                        "last_verified_at": datetime.now(UTC),
                        "updated_at": datetime.now(UTC),
                        "id": int((row or {}).get("id") or 0),
                    },
                )
        except HTTPException:
            # Preserve created record for manual recovery if Telegram API is temporarily unavailable.
            pass

        self.rbac.audit(
            org_id=normalized_org,
            team_id="",
            actor_user_id=actor_user_id,
            action="byob.bot.register",
            target_type="bot_connection",
            target_id=str(row.get("id") or ""),
            details={
                "provider": provider,
                "bot_name": bot_name,
                "external_bot_id": external_bot_id or "",
            },
        )
        return ByobBotSummary.model_validate(dict(row))

    def list_byob_bots(self, *, actor_user_id: int, org_id: str, provider: str | None = None) -> list[ByobBotSummary]:
        normalized_org = org_id.strip()
        if not normalized_org:
            raise HTTPException(status_code=400, detail="Organization ID is required")
        self._enforce_org_admin(actor_user_id=actor_user_id, org_id=normalized_org)

        normalized_provider = self._normalize_provider(provider or "telegram") if provider else None
        with self.engine.begin() as conn:
            if normalized_provider:
                rows = conn.execute(
                    text(
                        """
                        SELECT id, org_id, provider, bot_name, external_bot_id, token_hint, is_active,
                               created_at::text AS created_at, updated_at::text AS updated_at
                        FROM byob_bot_connections
                        WHERE org_id = :org_id AND provider = :provider
                        ORDER BY created_at DESC
                        """
                    ),
                    {
                        "org_id": normalized_org,
                        "provider": normalized_provider,
                    },
                ).mappings().all()
            else:
                rows = conn.execute(
                    text(
                        """
                        SELECT id, org_id, provider, bot_name, external_bot_id, token_hint, is_active,
                               created_at::text AS created_at, updated_at::text AS updated_at
                        FROM byob_bot_connections
                        WHERE org_id = :org_id
                        ORDER BY created_at DESC
                        """
                    ),
                    {"org_id": normalized_org},
                ).mappings().all()

        return [ByobBotSummary.model_validate(dict(row)) for row in rows]

    def update_byob_bot_status(
        self,
        *,
        actor_user_id: int,
        org_id: str,
        bot_id: int,
        payload: UpdateByobBotStatusRequest,
    ) -> ByobBotSummary:
        normalized_org = org_id.strip()
        if not normalized_org:
            raise HTTPException(status_code=400, detail="Organization ID is required")
        self._enforce_org_admin(actor_user_id=actor_user_id, org_id=normalized_org)

        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    UPDATE byob_bot_connections
                    SET is_active = :is_active, updated_at = :updated_at
                    WHERE id = :bot_id AND org_id = :org_id
                    RETURNING id, org_id, provider, bot_name, external_bot_id, token_hint, is_active,
                              created_at::text AS created_at, updated_at::text AS updated_at
                    """
                ),
                {
                    "is_active": bool(payload.is_active),
                    "updated_at": datetime.now(UTC),
                    "bot_id": int(bot_id),
                    "org_id": normalized_org,
                },
            ).mappings().first()

        if row is None:
            raise HTTPException(status_code=404, detail="Bot connection not found")

        if bool(payload.is_active):
            with self.engine.begin() as conn:
                token_row = conn.execute(
                    text(
                        """
                        SELECT token_ciphertext, webhook_secret
                        FROM byob_bot_connections
                        WHERE id = :bot_id AND org_id = :org_id
                        LIMIT 1
                        """
                    ),
                    {
                        "bot_id": int(bot_id),
                        "org_id": normalized_org,
                    },
                ).mappings().first()
            token = self._decode_bot_token(str((token_row or {}).get("token_ciphertext") or ""))
            if token:
                try:
                    self._set_telegram_webhook(
                        token=token,
                        org_id=normalized_org,
                        bot_id=int(bot_id),
                        webhook_secret=str((token_row or {}).get("webhook_secret") or ""),
                    )
                except HTTPException:
                    pass

        self.rbac.audit(
            org_id=normalized_org,
            team_id="",
            actor_user_id=actor_user_id,
            action="byob.bot.set_active",
            target_type="bot_connection",
            target_id=str(bot_id),
            details={"is_active": bool(payload.is_active)},
        )
        return ByobBotSummary.model_validate(dict(row))

    def _apply_credit_delta(
        self,
        *,
        actor_user_id: int,
        org_id: str,
        delta: int,
        reason: str,
        reference_type: str,
        reference_id: str,
    ) -> int:
        if delta == 0:
            raise HTTPException(status_code=400, detail="Credit adjustment delta must not be zero")

        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO org_credit_accounts (org_id, balance, updated_at)
                    VALUES (:org_id, 0, :updated_at)
                    ON CONFLICT (org_id) DO NOTHING
                    """
                ),
                {
                    "org_id": org_id,
                    "updated_at": now,
                },
            )

            row = conn.execute(
                text(
                    """
                    SELECT balance
                    FROM org_credit_accounts
                    WHERE org_id = :org_id
                    FOR UPDATE
                    """
                ),
                {"org_id": org_id},
            ).mappings().first()
            if row is None:
                raise HTTPException(status_code=404, detail="Organization credit account not found")

            current_balance = int(row.get("balance") or 0)
            next_balance = current_balance + int(delta)
            if next_balance < 0:
                raise HTTPException(status_code=409, detail="Insufficient credits")

            conn.execute(
                text(
                    """
                    UPDATE org_credit_accounts
                    SET balance = :balance, updated_at = :updated_at
                    WHERE org_id = :org_id
                    """
                ),
                {
                    "balance": next_balance,
                    "updated_at": now,
                    "org_id": org_id,
                },
            )

            conn.execute(
                text(
                    """
                    INSERT INTO org_credit_ledger (
                        org_id,
                        delta,
                        balance_after,
                        reason,
                        actor_user_id,
                        reference_type,
                        reference_id,
                        created_at
                    )
                    VALUES (
                        :org_id,
                        :delta,
                        :balance_after,
                        :reason,
                        :actor_user_id,
                        :reference_type,
                        :reference_id,
                        :created_at
                    )
                    """
                ),
                {
                    "org_id": org_id,
                    "delta": int(delta),
                    "balance_after": next_balance,
                    "reason": reason,
                    "actor_user_id": int(actor_user_id),
                    "reference_type": reference_type,
                    "reference_id": reference_id,
                    "created_at": now,
                },
            )

        return next_balance

    def _get_credit_policy(self, *, org_id: str) -> CreditPolicyResponse:
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT daily_limit, monthly_limit, low_balance_threshold
                    FROM org_credit_policies
                    WHERE org_id = :org_id
                    LIMIT 1
                    """
                ),
                {"org_id": org_id},
            ).mappings().first()

        if row is None:
            return CreditPolicyResponse(org_id=org_id, daily_limit=None, monthly_limit=None, low_balance_threshold=0)

        return CreditPolicyResponse(
            org_id=org_id,
            daily_limit=int(row["daily_limit"]) if row.get("daily_limit") is not None else None,
            monthly_limit=int(row["monthly_limit"]) if row.get("monthly_limit") is not None else None,
            low_balance_threshold=int(row.get("low_balance_threshold") or 0),
        )

    def set_org_credit_policy(
        self,
        *,
        actor_user_id: int,
        org_id: str,
        payload: UpdateCreditPolicyRequest,
    ) -> CreditPolicyResponse:
        normalized_org = org_id.strip()
        if not normalized_org:
            raise HTTPException(status_code=400, detail="Organization ID is required")
        self._enforce_org_admin(actor_user_id=actor_user_id, org_id=normalized_org)

        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO org_credit_policies (
                        org_id,
                        daily_limit,
                        monthly_limit,
                        low_balance_threshold,
                        updated_at
                    )
                    VALUES (
                        :org_id,
                        :daily_limit,
                        :monthly_limit,
                        :low_balance_threshold,
                        :updated_at
                    )
                    ON CONFLICT (org_id)
                    DO UPDATE SET
                        daily_limit = EXCLUDED.daily_limit,
                        monthly_limit = EXCLUDED.monthly_limit,
                        low_balance_threshold = EXCLUDED.low_balance_threshold,
                        updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "org_id": normalized_org,
                    "daily_limit": payload.daily_limit,
                    "monthly_limit": payload.monthly_limit,
                    "low_balance_threshold": int(payload.low_balance_threshold),
                    "updated_at": datetime.now(UTC),
                },
            )

        self.rbac.audit(
            org_id=normalized_org,
            team_id="",
            actor_user_id=actor_user_id,
            action="billing.policy.update",
            target_type="credit_policy",
            target_id=normalized_org,
            details={
                "daily_limit": payload.daily_limit,
                "monthly_limit": payload.monthly_limit,
                "low_balance_threshold": int(payload.low_balance_threshold),
            },
        )
        return self._get_credit_policy(org_id=normalized_org)

    def get_org_credit_policy(self, *, actor_user_id: int, org_id: str) -> CreditPolicyResponse:
        normalized_org = org_id.strip()
        if not normalized_org:
            raise HTTPException(status_code=400, detail="Organization ID is required")
        self._enforce_org_admin(actor_user_id=actor_user_id, org_id=normalized_org)
        return self._get_credit_policy(org_id=normalized_org)

    def _enforce_credit_limits_before_debit(self, *, org_id: str, amount: int) -> None:
        if amount <= 0:
            return

        policy = self._get_credit_policy(org_id=org_id)
        if policy.daily_limit is None and policy.monthly_limit is None:
            return

        with self.engine.begin() as conn:
            day_row = conn.execute(
                text(
                    """
                    SELECT COALESCE(SUM(-delta), 0) AS spent
                    FROM org_credit_ledger
                    WHERE org_id = :org_id
                      AND delta < 0
                      AND created_at >= date_trunc('day', now())
                    """
                ),
                {"org_id": org_id},
            ).mappings().first()
            month_row = conn.execute(
                text(
                    """
                    SELECT COALESCE(SUM(-delta), 0) AS spent
                    FROM org_credit_ledger
                    WHERE org_id = :org_id
                      AND delta < 0
                      AND created_at >= date_trunc('month', now())
                    """
                ),
                {"org_id": org_id},
            ).mappings().first()

        day_spent = int((day_row or {}).get("spent") or 0)
        month_spent = int((month_row or {}).get("spent") or 0)

        if policy.daily_limit is not None and (day_spent + amount) > int(policy.daily_limit):
            raise HTTPException(status_code=409, detail="Daily credit limit exceeded")
        if policy.monthly_limit is not None and (month_spent + amount) > int(policy.monthly_limit):
            raise HTTPException(status_code=409, detail="Monthly credit limit exceeded")

    def _debit_org_for_usage(
        self,
        *,
        org_id: str,
        actor_user_id: int,
        total_tokens: int,
        reference_type: str,
        reference_id: str,
    ) -> int:
        usage_tokens = max(0, int(total_tokens))
        usage_credits = max(1, (usage_tokens + 999) // 1000)
        self._enforce_credit_limits_before_debit(org_id=org_id, amount=usage_credits)
        balance = self._apply_credit_delta(
            actor_user_id=actor_user_id,
            org_id=org_id,
            delta=-usage_credits,
            reason=f"LLM usage ({usage_tokens} tokens)",
            reference_type=reference_type,
            reference_id=reference_id,
        )

        policy = self._get_credit_policy(org_id=org_id)
        if balance <= int(policy.low_balance_threshold):
            self.rbac.audit(
                org_id=org_id,
                team_id="",
                actor_user_id=actor_user_id,
                action="billing.low_balance",
                target_type="credit_account",
                target_id=org_id,
                details={
                    "balance": balance,
                    "threshold": int(policy.low_balance_threshold),
                },
            )
        return balance

    def process_telegram_webhook(
        self,
        *,
        org_id: str,
        bot_id: int,
        webhook_secret: str,
        client_ip: str | None,
        update: TelegramWebhookUpdate,
    ) -> dict[str, Any]:
        normalized_org = org_id.strip()
        if not normalized_org:
            raise HTTPException(status_code=400, detail="Organization ID is required")
        self._enforce_telegram_ip(client_ip=client_ip)

        with self.engine.begin() as conn:
            bot_row = conn.execute(
                text(
                    """
                    SELECT id, org_id, bot_name, is_active, webhook_secret
                    FROM byob_bot_connections
                    WHERE id = :bot_id AND org_id = :org_id
                    LIMIT 1
                    """
                ),
                {
                    "bot_id": int(bot_id),
                    "org_id": normalized_org,
                },
            ).mappings().first()

        if bot_row is None:
            raise HTTPException(status_code=404, detail="Bot connection not found")
        if not bool(bot_row.get("is_active")):
            raise HTTPException(status_code=409, detail="Bot connection is inactive")

        expected_secret = str(bot_row.get("webhook_secret") or "")
        if not expected_secret or not secrets.compare_digest(expected_secret, webhook_secret or ""):
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

        message = update.message or {}
        chat_obj = message.get("chat") if isinstance(message, dict) else None
        from_obj = message.get("from") if isinstance(message, dict) else None
        text_message = str(message.get("text") or "").strip() if isinstance(message, dict) else ""

        if not isinstance(chat_obj, dict) or not isinstance(from_obj, dict) or not text_message:
            return {"ok": True}

        chat_id = int(chat_obj.get("id") or 0)
        user_id = int(from_obj.get("id") or 0)
        if chat_id == 0 or user_id <= 0:
            return {"ok": True}

        self._consume_rate_limit(
            scope="tenant",
            scope_key=normalized_org,
            limit_per_minute=int(self.settings.byob_tenant_rate_limit_per_min),
        )
        self._consume_rate_limit(
            scope="tenant_user",
            scope_key=f"{normalized_org}:{int(user_id)}",
            limit_per_minute=int(self.settings.byob_user_rate_limit_per_min),
        )

        role = self.rbac.get_role(org_id=normalized_org, user_id=user_id, fallback_role="member")
        if self.settings.enable_dynamic_tools:
            self.agent.refresh_dynamic_tools()
            all_dynamic = set(self.agent.registry.tools.keys())
        else:
            all_dynamic = set()
        allowed_dynamic = self.rbac.resolve_allowed_skills(
            org_id=normalized_org,
            user_id=user_id,
            role=role,
            all_dynamic_tools=all_dynamic,
        )

        try:
            answer_result = self.agent.run(
                text_message,
                [],
                chat_id,
                normalized_org,
                f"bot:{int(bot_id)}",
                user_id,
                role,
                allowed_dynamic,
            )
            answer = answer_result.answer or "I could not generate a response."
            _ = self._debit_org_for_usage(
                org_id=normalized_org,
                actor_user_id=user_id,
                total_tokens=int(answer_result.token_usage.total_tokens),
                reference_type="telegram_webhook",
                reference_id=str(update.update_id or chat_id),
            )
        except HTTPException as exc:
            if exc.status_code in {409}:
                answer = "Credit policy limit reached. Please contact bot owner."
            else:
                answer = "Assistant is temporarily unavailable."
        except Exception:
            answer = "Assistant is temporarily unavailable."

        queued_id = self._queue_byob_outbound_message(
            org_id=normalized_org,
            bot_connection_id=int(bot_id),
            chat_id=chat_id,
            payload={
                "chat_id": chat_id,
                "text": answer,
            },
        )
        if queued_id > 0:
            self._deliver_byob_outbound_queue(org_id=normalized_org, bot_connection_id=int(bot_id))
        return {"ok": True}

    def get_org_credit_balance(self, *, actor_user_id: int, org_id: str) -> OrgCreditBalanceResponse:
        normalized_org = org_id.strip()
        if not normalized_org:
            raise HTTPException(status_code=400, detail="Organization ID is required")
        self._enforce_org_admin(actor_user_id=actor_user_id, org_id=normalized_org)

        with self.engine.begin() as conn:
            row = conn.execute(
                text("SELECT balance FROM org_credit_accounts WHERE org_id = :org_id LIMIT 1"),
                {"org_id": normalized_org},
            ).mappings().first()

        return OrgCreditBalanceResponse(org_id=normalized_org, balance=int((row or {}).get("balance") or 0))

    def top_up_org_credits(self, *, actor_user_id: int, org_id: str, payload: CreditAdjustmentRequest) -> OrgCreditBalanceResponse:
        normalized_org = org_id.strip()
        if not normalized_org:
            raise HTTPException(status_code=400, detail="Organization ID is required")
        self._enforce_org_admin(actor_user_id=actor_user_id, org_id=normalized_org)

        balance = self._apply_credit_delta(
            actor_user_id=actor_user_id,
            org_id=normalized_org,
            delta=int(payload.amount),
            reason=payload.reason.strip(),
            reference_type=payload.reference_type.strip(),
            reference_id=payload.reference_id.strip(),
        )
        self.rbac.audit(
            org_id=normalized_org,
            team_id="",
            actor_user_id=actor_user_id,
            action="billing.credit_topup",
            target_type="credit_account",
            target_id=normalized_org,
            details={"amount": int(payload.amount), "balance": int(balance)},
        )
        return OrgCreditBalanceResponse(org_id=normalized_org, balance=balance)

    def debit_org_credits(self, *, actor_user_id: int, org_id: str, payload: CreditAdjustmentRequest) -> OrgCreditBalanceResponse:
        normalized_org = org_id.strip()
        if not normalized_org:
            raise HTTPException(status_code=400, detail="Organization ID is required")
        self._enforce_org_admin(actor_user_id=actor_user_id, org_id=normalized_org)
        self._enforce_credit_limits_before_debit(org_id=normalized_org, amount=int(payload.amount))

        balance = self._apply_credit_delta(
            actor_user_id=actor_user_id,
            org_id=normalized_org,
            delta=-int(payload.amount),
            reason=payload.reason.strip(),
            reference_type=payload.reference_type.strip(),
            reference_id=payload.reference_id.strip(),
        )
        self.rbac.audit(
            org_id=normalized_org,
            team_id="",
            actor_user_id=actor_user_id,
            action="billing.credit_debit",
            target_type="credit_account",
            target_id=normalized_org,
            details={"amount": int(payload.amount), "balance": int(balance)},
        )
        return OrgCreditBalanceResponse(org_id=normalized_org, balance=balance)

    def list_org_credit_ledger(self, *, actor_user_id: int, org_id: str, limit: int = 50) -> CreditLedgerResponse:
        normalized_org = org_id.strip()
        if not normalized_org:
            raise HTTPException(status_code=400, detail="Organization ID is required")
        self._enforce_org_admin(actor_user_id=actor_user_id, org_id=normalized_org)

        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT id, delta, balance_after, reason, actor_user_id, reference_type, reference_id, created_at::text AS created_at
                    FROM org_credit_ledger
                    WHERE org_id = :org_id
                    ORDER BY created_at DESC
                    LIMIT :limit
                    """
                ),
                {
                    "org_id": normalized_org,
                    "limit": max(1, min(int(limit), 500)),
                },
            ).mappings().all()

        return CreditLedgerResponse(
            org_id=normalized_org,
            items=[CreditLedgerItem.model_validate(dict(row)) for row in rows],
        )


    def set_role(self, *, actor_user_id: int, target_user_id: int, payload: SetRoleRequest) -> None:
        self._enforce_org_admin(actor_user_id=actor_user_id, org_id=payload.org_id)
        self.rbac.set_role(
            org_id=payload.org_id,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            role=payload.role,
        )

    def list_org_users(self, *, actor_user_id: int, org_id: str, team_id: str | None = None) -> list[AdminUserSummary]:
        self._enforce_org_admin(actor_user_id=actor_user_id, org_id=org_id)
        supports_otp = self._supports_auth_otp_fields()
        force_password_select = "COALESCE(u.force_password_change, FALSE) AS force_password_change" if supports_otp else "FALSE AS force_password_change"
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        u.user_id,
                        u.email,
                        u.full_name,
                        u.title,
                        u.profile_bio,
                        COALESCE(au.role, 'member') AS role,
                        """
                                        + force_password_select
                                        +
                                        """,
                                                TRUE AS in_team
                    FROM org_memberships om
                    JOIN auth_users u
                      ON u.user_id = om.user_id
                    LEFT JOIN app_users au
                      ON au.org_id = om.org_id AND au.user_id = om.user_id
                    WHERE om.org_id = :org_id
                    ORDER BY u.user_id ASC
                    """
                ),
                {
                    "org_id": org_id,
                },
            ).mappings().all()

        result: list[AdminUserSummary] = []
        for row in rows:
            result.append(
                AdminUserSummary(
                    user_id=int(row.get("user_id") or 0),
                    email=str(row.get("email") or ""),
                    full_name=str(row.get("full_name") or ""),
                    title=str(row.get("title") or ""),
                    profile_bio=str(row.get("profile_bio") or ""),
                    role=str(row.get("role") or "member"),
                    in_team=bool(row.get("in_team")),
                    force_password_change=bool(row.get("force_password_change")),
                )
            )
        return result

    def list_all_users(self, *, actor_user_id: int) -> list[GlobalAdminUserSummary]:
        self._enforce_admin(actor_user_id)
        with self.engine.begin() as conn:
            user_rows = conn.execute(
                text(
                    """
                    SELECT user_id, email, full_name, title, profile_bio
                    FROM auth_users
                    ORDER BY user_id ASC
                    """
                )
            ).mappings().all()
            membership_rows = conn.execute(
                text(
                    """
                    SELECT user_id, org_id
                    FROM org_memberships
                    ORDER BY user_id ASC, org_id ASC
                    """
                )
            ).mappings().all()

        membership_map: dict[int, list[str]] = {}
        for row in membership_rows:
            uid = int(row.get("user_id") or 0)
            org = str(row.get("org_id") or "").strip()
            if uid <= 0 or not org:
                continue
            membership_map.setdefault(uid, []).append(org)

        result: list[GlobalAdminUserSummary] = []
        for row in user_rows:
            uid = int(row.get("user_id") or 0)
            if uid <= 0:
                continue
            result.append(
                GlobalAdminUserSummary(
                    user_id=uid,
                    email=str(row.get("email") or ""),
                    full_name=str(row.get("full_name") or ""),
                    title=str(row.get("title") or ""),
                    profile_bio=str(row.get("profile_bio") or ""),
                    organization_ids=membership_map.get(uid, []),
                )
            )
        return result

    def bind_user_to_org(
        self,
        *,
        actor_user_id: int,
        target_user_id: int,
        payload: MembershipUpsertRequest,
    ) -> dict[str, Any]:
        self._enforce_org_admin(actor_user_id=actor_user_id, org_id=payload.org_id)
        org_id = payload.org_id.strip()
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID is required")

        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            user_exists = conn.execute(
                text("SELECT 1 FROM auth_users WHERE user_id = :user_id LIMIT 1"),
                {"user_id": int(target_user_id)},
            ).first()
            if user_exists is None:
                raise HTTPException(status_code=404, detail="User not found")

            existing = conn.execute(
                text(
                    """
                    SELECT 1
                    FROM org_memberships
                    WHERE org_id = :org_id AND user_id = :user_id
                    LIMIT 1
                    """
                ),
                {
                    "org_id": org_id,
                    "user_id": int(target_user_id),
                },
            ).first()

            conn.execute(
                text(
                    """
                    INSERT INTO org_memberships (org_id, user_id, created_at)
                    VALUES (:org_id, :user_id, :created_at)
                    ON CONFLICT (org_id, user_id) DO NOTHING
                    """
                ),
                {
                    "org_id": org_id,
                    "user_id": int(target_user_id),
                    "created_at": now,
                },
            )

        self.rbac.upsert_user(org_id=org_id, user_id=int(target_user_id), role=payload.role)
        self.rbac.audit(
            org_id=org_id,
            team_id="",
            actor_user_id=actor_user_id,
            action="rbac.bind_user_org",
            target_type="user",
            target_id=str(target_user_id),
            details={"created": existing is None, "role": payload.role},
        )
        return {"status": "ok", "created": existing is None}

    def unbind_user_from_org(self, *, actor_user_id: int, target_user_id: int, org_id: str) -> dict[str, Any]:
        normalized_org = org_id.strip()
        if not normalized_org:
            raise HTTPException(status_code=400, detail="Organization ID is required")
        self._enforce_org_admin(actor_user_id=actor_user_id, org_id=normalized_org)

        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    DELETE FROM app_users
                    WHERE org_id = :org_id AND user_id = :user_id
                    """
                ),
                {
                    "org_id": normalized_org,
                    "user_id": int(target_user_id),
                },
            )
            deleted_membership = conn.execute(
                text(
                    """
                    DELETE FROM org_memberships
                    WHERE org_id = :org_id AND user_id = :user_id
                    """
                ),
                {
                    "org_id": normalized_org,
                    "user_id": int(target_user_id),
                },
            )

        removed = int(deleted_membership.rowcount or 0) > 0
        self.rbac.audit(
            org_id=normalized_org,
            team_id="",
            actor_user_id=actor_user_id,
            action="rbac.unbind_user_org",
            target_type="user",
            target_id=str(target_user_id),
            details={"removed": removed},
        )
        return {"status": "ok", "removed": removed}

    def grant_skill(self, *, actor_user_id: int, target_user_id: int, payload: SkillAssignmentRequest) -> None:
        self._enforce_org_admin(actor_user_id=actor_user_id, org_id=payload.org_id)
        self.rbac.assign_skill(
            org_id=payload.org_id,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            tool_name=payload.tool_name,
        )

    def revoke_skill(self, *, actor_user_id: int, target_user_id: int, payload: SkillAssignmentRequest) -> bool:
        self._enforce_org_admin(actor_user_id=actor_user_id, org_id=payload.org_id)
        return self.rbac.revoke_skill(
            org_id=payload.org_id,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            tool_name=payload.tool_name,
        )

    def list_user_skills(self, *, actor_user_id: int, target_user_id: int, org_id: str) -> list[str]:
        self._enforce_org_admin(actor_user_id=actor_user_id, org_id=org_id)
        return self.rbac.list_user_skills(org_id=org_id, user_id=target_user_id)

    def list_dynamic_skills(self, *, actor_user_id: int) -> list[DynamicSkillSummary]:
        self._enforce_admin(actor_user_id)
        skills: list[DynamicSkillSummary] = []
        root = self.settings.dynamic_skills_dir
        if not root.exists():
            return skills

        for path in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_dir():
                continue
            manifest_path = path / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                manifest = {}
            tool_name = str(manifest.get("name") or path.name)
            description = str(manifest.get("description") or "")
            skills.append(DynamicSkillSummary(folder=path.name, tool_name=tool_name, description=description))
        return skills

    def convert_claude_markdown_to_skill(
        self,
        *,
        actor_user_id: int,
        payload: ClaudeSkillConvertRequest,
    ) -> dict[str, str]:
        self._enforce_admin(actor_user_id)
        meta, body = _parse_claude_markdown(payload.markdown)

        requested_name = (payload.skill_name or "").strip()
        source_name = requested_name or meta.get("name", "")
        if not source_name:
            source_name = "generated_skill"

        tool_name = _slugify_skill_name(source_name)
        skill_dir = (self.settings.dynamic_skills_dir / tool_name).resolve()
        root = self.settings.dynamic_skills_dir.resolve()
        try:
            skill_dir.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid skill name") from exc

        if skill_dir.exists() and not payload.overwrite:
            raise HTTPException(
                status_code=409,
                detail=f"Skill '{tool_name}' already exists. Use overwrite=true to replace.",
            )

        if skill_dir.exists() and payload.overwrite:
            shutil.rmtree(skill_dir)
        skill_dir.mkdir(parents=True, exist_ok=True)

        display_name = meta.get("name", source_name).strip() or source_name
        description = meta.get("description", "").strip() or f"Generated from Claude Agent: {display_name}"

        tool_content = _build_generated_tool_py(
            function_name=tool_name,
            tool_name=tool_name,
            instruction_text=body,
        )
        skill_content = _build_generated_skill_md(
            display_name=display_name,
            description=description,
            source_body=body,
        )
        manifest = _build_manifest(tool_name=tool_name, description=description)

        (skill_dir / "tool.py").write_text(tool_content, encoding="utf-8")
        (skill_dir / "skill.md").write_text(skill_content, encoding="utf-8")
        final_manifest = _attach_manifest_hashes(skill_dir, manifest)
        (skill_dir / "manifest.json").write_text(
            json.dumps(final_manifest, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )

        if self.settings.enable_dynamic_tools:
            self.agent.refresh_dynamic_tools()

        return {
            "status": "ok",
            "skill_name": tool_name,
            "skill_dir": str(skill_dir),
        }

    def delete_dynamic_skill(self, *, actor_user_id: int, skill_name: str) -> bool:
        self._enforce_admin(actor_user_id)
        normalized = _slugify_skill_name(skill_name)
        root = self.settings.dynamic_skills_dir.resolve()
        target = (root / normalized).resolve()

        try:
            target.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid skill name") from exc

        if not target.exists() or not target.is_dir():
            return False

        shutil.rmtree(target)
        if self.settings.enable_dynamic_tools:
            self.agent.refresh_dynamic_tools()
        return True

    def preview_bulk_claude_conversion(
        self,
        *,
        actor_user_id: int,
        files: list[tuple[str, str]],
        skill_name_prefix: str | None,
    ) -> BulkClaudeDryRunResponse:
        self._enforce_admin(actor_user_id)
        root = self.settings.dynamic_skills_dir.resolve()
        prefix = (skill_name_prefix or "").strip()

        valid = 0
        invalid = 0
        results: list[BulkClaudeDryRunItem] = []

        for filename, markdown in files:
            try:
                meta, _ = _parse_claude_markdown(markdown)
                stem = Path(filename).stem.strip()
                source_name = stem or meta.get("name", "") or "generated_skill"
                raw_name = f"{prefix}_{source_name}" if prefix else source_name
                proposed = _slugify_skill_name(raw_name)
                exists = (root / proposed).exists()
                valid += 1
                results.append(
                    BulkClaudeDryRunItem(
                        filename=filename,
                        status="ok",
                        proposed_skill_name=proposed,
                        exists=exists,
                    )
                )
            except Exception as exc:
                invalid += 1
                results.append(
                    BulkClaudeDryRunItem(
                        filename=filename,
                        status="error",
                        error=str(exc),
                    )
                )

        return BulkClaudeDryRunResponse(total=len(files), valid=valid, invalid=invalid, results=results)

    def _assert_membership(self, *, org_id: str, team_id: str, user_id: int) -> None:
        with self.engine.begin() as conn:
            member = conn.execute(
                text(
                    """
                    SELECT 1
                    FROM team_members
                    WHERE org_id = :org_id AND team_id = :team_id AND user_id = :user_id
                    LIMIT 1
                    """
                ),
                {
                    "org_id": org_id,
                    "team_id": team_id,
                    "user_id": int(user_id),
                },
            ).first()
        if member is None:
            raise HTTPException(status_code=403, detail="User is not a member of the target team")

    def _load_group_history(self, *, user_id: int, limit: int = GROUP_SHORT_MEMORY_LIMIT) -> list[dict[str, str]]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT sender_type, content
                    FROM group_messages
                    WHERE scope_user_id = :scope_user_id
                    ORDER BY created_at DESC
                    LIMIT :limit
                    """
                ),
                {
                    "scope_user_id": int(user_id),
                    "limit": max(1, int(limit)),
                },
            ).mappings().all()

        history: list[dict[str, str]] = []
        for row in reversed(rows):
            sender_type = str(row.get("sender_type") or "user")
            role = "assistant" if sender_type == "assistant" else "user"
            content = str(row.get("content") or "")
            if content:
                history.append({"role": role, "content": content})
        return history

    def _append_group_message(
        self,
        *,
        user_id: int,
        sender_user_id: int | None,
        sender_type: str,
        content: str,
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO group_messages (scope_user_id, sender_user_id, sender_type, content, created_at)
                    VALUES (:scope_user_id, :sender_user_id, :sender_type, :content, :created_at)
                    """
                ),
                {
                    "scope_user_id": int(user_id),
                    "sender_user_id": int(sender_user_id) if sender_user_id is not None else None,
                    "sender_type": sender_type,
                    "content": content,
                    "created_at": datetime.now(UTC),
                },
            )

    def _read_group_messages(self, *, user_id: int, limit: int = 30) -> list[ChatMessage]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT sender_type, sender_user_id, content, created_at::text AS created_at
                    FROM group_messages
                    WHERE scope_user_id = :scope_user_id
                    ORDER BY created_at DESC
                    LIMIT :limit
                    """
                ),
                {
                    "scope_user_id": int(user_id),
                    "limit": max(1, int(limit)),
                },
            ).mappings().all()
        return [
            ChatMessage(
                sender_type=str(row.get("sender_type") or "user"),
                sender_user_id=int(row["sender_user_id"]) if row.get("sender_user_id") is not None else None,
                content=str(row.get("content") or ""),
                created_at=str(row.get("created_at") or ""),
            )
            for row in reversed(rows)
        ]

    def _recall_shared_memory(self, *, user_id: int, query: str) -> str:
        if self.long_term is None:
            return ""

        combined: list[str] = []
        try:
            shared = self.long_term.recall(
                org_id=USER_SCOPE_ORG_ID,
                user_id=0,
                chat_id=_scope_chat_id(user_id),
                query_text=query,
                limit=3,
            )
            personal = self.long_term.recall(
                org_id=USER_SCOPE_ORG_ID,
                user_id=user_id,
                chat_id=_scope_chat_id(user_id),
                query_text=query,
                limit=3,
            )
            for item in [*shared, *personal]:
                if item.content not in combined:
                    combined.append(item.content)
        except Exception:
            return ""

        if not combined:
            return ""
        lines = [
            "Shared group memory and user profile context:",
        ]
        for idx, item in enumerate(combined, start=1):
            lines.append(f"{idx}. {item}")
        return "\n".join(lines)

    def send_group_chat(self, *, user_id: int, payload: ChatRequest) -> ChatResponse:
        org_id = USER_SCOPE_ORG_ID
        team_id = _user_scope_id(user_id)

        role = self.rbac.get_role(org_id=org_id, user_id=user_id, fallback_role="member")
        if self.settings.enable_dynamic_tools:
            self.agent.refresh_dynamic_tools()
            all_dynamic = set(self.agent.registry.tools.keys())
        else:
            all_dynamic = set()
        allowed_dynamic = self.rbac.resolve_allowed_skills(
            org_id=org_id,
            user_id=user_id,
            role=role,
            all_dynamic_tools=all_dynamic,
        )

        user_profile = self._user_profile_text(user_id=user_id)
        try:
            history = self._load_group_history(user_id=user_id)
        except Exception:
            history = []
        memory_context = self._recall_shared_memory(user_id=user_id, query=payload.message)
        if user_profile:
            history = [{"role": "system", "content": user_profile}, *history]
        if memory_context:
            history = [{"role": "system", "content": memory_context}, *history]

        try:
            answer_result = self.agent.run(
                payload.message,
                history,
                _scope_chat_id(user_id),
                org_id,
                team_id,
                user_id,
                role,
                allowed_dynamic,
            )
            answer = answer_result.answer or "I could not generate a response."
            file_payload = _extract_file_payload_from_tool_messages(answer_result.messages)
            if file_payload is not None:
                answer = json.dumps(file_payload, ensure_ascii=True)
        except Exception:
            answer = "Assistant is temporarily unavailable. Please try again in a moment."

        try:
            self._append_group_message(
                user_id=user_id,
                sender_user_id=user_id,
                sender_type="user",
                content=payload.message,
            )
            self._append_group_message(
                user_id=user_id,
                sender_user_id=None,
                sender_type="assistant",
                content=answer,
            )
        except Exception:
            pass

        if self.long_term is not None:
            try:
                self.long_term.remember(
                    org_id=org_id,
                    user_id=user_id,
                    chat_id=_scope_chat_id(user_id),
                    user_text=payload.message,
                    assistant_text=answer,
                    source="api-chat",
                )
                self.long_term.remember(
                    org_id=org_id,
                    user_id=0,
                    chat_id=_scope_chat_id(user_id),
                    user_text=payload.message,
                    assistant_text=answer,
                    source="api-group",
                )
            except Exception:
                pass

        try:
            messages = self._read_group_messages(user_id=user_id)
        except Exception:
            now_iso = datetime.now(UTC).isoformat()
            messages = [
                ChatMessage(
                    sender_type="user",
                    sender_user_id=user_id,
                    content=payload.message,
                    created_at=now_iso,
                ),
                ChatMessage(
                    sender_type="assistant",
                    sender_user_id=None,
                    content=answer,
                    created_at=now_iso,
                ),
            ]
        return ChatResponse(answer=answer, messages=messages)

    def _user_profile_text(self, *, user_id: int) -> str:
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT full_name, title, profile_bio
                    FROM auth_users
                    WHERE user_id = :user_id
                    LIMIT 1
                    """
                ),
                {"user_id": int(user_id)},
            ).mappings().first()
        if row is None:
            return ""
        full_name = str(row.get("full_name") or "").strip()
        title = str(row.get("title") or "").strip()
        bio = str(row.get("profile_bio") or "").strip()
        if not (full_name or title or bio):
            return ""
        return (
            "User profile context:\n"
            f"- Name: {full_name or 'Unknown'}\n"
            f"- Role/Title: {title or 'Unknown'}\n"
            f"- About: {bio or 'Not provided'}"
        )

settings = load_settings()
service = ApiService(settings)
chat_hub = RealtimeChatHub(settings)


async def _run_byob_delivery_worker() -> None:
    interval = max(1, int(settings.byob_delivery_poll_interval_seconds))
    while True:
        try:
            await asyncio.to_thread(service.process_byob_delivery_backlog)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(interval)


@contextlib.asynccontextmanager
async def _lifespan(_: FastAPI):
    await chat_hub.startup()
    delivery_worker = asyncio.create_task(_run_byob_delivery_worker(), name="byob-delivery-worker")
    try:
        yield
    finally:
        delivery_worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await delivery_worker
        await chat_hub.shutdown()


app = FastAPI(title=APP_NAME, lifespan=_lifespan)


def _extract_bearer_token(authorization: str) -> str:
    raw = authorization.strip()
    if not raw.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Authorization header must be Bearer token")
    token = raw[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token is empty")
    return token


def get_current_user(authorization: Annotated[str, Header(alias="Authorization")]) -> dict[str, Any]:
    token = _extract_bearer_token(authorization)
    return service.get_user_from_token(token)


def _legacy_scope_removed() -> NoReturn:
    raise HTTPException(
        status_code=410,
        detail="Organization/team functionality was removed. Use user_id-only endpoints.",
    )


def _build_chat_snapshot_payload(
    *,
    user_id: int,
    messages: Sequence[ChatMessage],
    client_message_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "chat.snapshot",
        "user_id": int(user_id),
        "messages": [message.model_dump() for message in messages],
    }
    if client_message_id:
        payload["client_message_id"] = client_message_id
    return payload


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


CurrentUser = Annotated[dict[str, Any], Depends(get_current_user)]


@app.post("/api/v1/auth/register", responses={409: {"description": "Email already registered"}})
def register(payload: RegisterRequest) -> dict[str, Any]:
    user_id = service.register_user(payload)
    return {"status": "registered", "user_id": user_id}


@app.post("/api/v1/auth/login", responses={401: {"description": "Invalid credentials"}})
def login(payload: LoginRequest) -> TokenResponse:
    return service.create_token(email=payload.email, password=payload.password)


@app.post("/api/v1/users/me/telegram")
def link_telegram(payload: TelegramLinkRequest, user: CurrentUser) -> dict[str, str]:
    service.link_telegram(user_id=int(user["user_id"]), telegram_id=payload.telegram_id)
    return {"status": "linked"}


@app.post("/api/v1/users/me/password/change", responses={401: {"description": "Current password is invalid"}})
def change_my_password(payload: PasswordChangeRequest, user: CurrentUser) -> dict[str, str]:
    service.change_password(
        user_id=int(user["user_id"]),
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    return {"status": "ok"}


@app.post("/api/v1/admin/organizations", responses={403: {"description": "Admin access required"}})
def create_org(payload: CreateOrgRequest, user: CurrentUser) -> dict[str, str]:
    service.create_org(actor_user_id=int(user["user_id"]), payload=payload)
    return {"status": "ok"}


@app.get("/api/v1/admin/organizations", responses={403: {"description": "Admin access required"}})
def list_organizations(user: CurrentUser) -> list[OrganizationSummary]:
    _legacy_scope_removed()


@app.get("/api/v1/admin/users", responses={403: {"description": "Admin access required"}})
def list_org_users(
    org_id: str,
    user: CurrentUser,
    team_id: str | None = None,
) -> list[AdminUserSummary]:
    _legacy_scope_removed()


@app.get("/api/v1/admin/users/all", responses={403: {"description": "Admin access required"}})
def list_all_users(user: CurrentUser) -> list[GlobalAdminUserSummary]:
    return service.list_all_users(actor_user_id=int(user["user_id"]))


@app.post("/api/v1/admin/users", responses={403: {"description": "Admin access required"}, 409: {"description": "Email already registered"}})
def create_user_by_admin(payload: AdminCreateUserRequest, user: CurrentUser) -> dict[str, Any]:
    _legacy_scope_removed()


@app.patch("/api/v1/admin/users/{target_user_id}", responses={403: {"description": "Admin access required"}})
def update_user_by_admin(target_user_id: int, payload: AdminUpdateUserRequest, user: CurrentUser) -> dict[str, str]:
    _legacy_scope_removed()


@app.post("/api/v1/admin/users/{target_user_id}/otp", responses={403: {"description": "Admin access required"}})
def reset_user_otp(target_user_id: int, payload: ResetPasswordRequest, user: CurrentUser) -> dict[str, Any]:
    _legacy_scope_removed()


@app.post("/api/v1/admin/users/{target_user_id}/force-password-change", responses={403: {"description": "Admin access required"}})
def force_user_password_change(target_user_id: int, payload: ForcePasswordChangeRequest, user: CurrentUser) -> dict[str, Any]:
    _legacy_scope_removed()


@app.post("/api/v1/admin/users/{target_user_id}/organizations/add", responses={403: {"description": "Admin access required"}})
def bind_user_to_org(target_user_id: int, payload: MembershipUpsertRequest, user: CurrentUser) -> dict[str, Any]:
    _legacy_scope_removed()


@app.delete("/api/v1/admin/users/{target_user_id}/organizations/{org_id}", responses={403: {"description": "Admin access required"}})
def unbind_user_from_org(target_user_id: int, org_id: str, user: CurrentUser) -> dict[str, Any]:
    _legacy_scope_removed()


@app.post("/api/v1/admin/users/{target_user_id}/role", responses={403: {"description": "Admin access required"}})
def set_role(
    target_user_id: int,
    payload: SetRoleRequest,
    user: CurrentUser,
) -> dict[str, str]:
    service.set_role(actor_user_id=int(user["user_id"]), target_user_id=target_user_id, payload=payload)
    return {"status": "ok"}


@app.post(
    "/api/v1/admin/users/{target_user_id}/skills/grant",
    responses={403: {"description": "Admin access required"}},
)
def grant_skill(
    target_user_id: int,
    payload: SkillAssignmentRequest,
    user: CurrentUser,
) -> dict[str, str]:
    service.grant_skill(actor_user_id=int(user["user_id"]), target_user_id=target_user_id, payload=payload)
    return {"status": "ok"}


@app.post(
    "/api/v1/admin/users/{target_user_id}/skills/revoke",
    responses={403: {"description": "Admin access required"}},
)
def revoke_skill(
    target_user_id: int,
    payload: SkillAssignmentRequest,
    user: CurrentUser,
) -> dict[str, Any]:
    removed = service.revoke_skill(actor_user_id=int(user["user_id"]), target_user_id=target_user_id, payload=payload)
    return {"status": "ok", "removed": removed}


@app.get("/api/v1/admin/users/{target_user_id}/skills", responses={403: {"description": "Admin access required"}})
def list_user_skills(
    target_user_id: int,
    org_id: str,
    user: CurrentUser,
) -> dict[str, Any]:
    skills = service.list_user_skills(
        actor_user_id=int(user["user_id"]),
        target_user_id=target_user_id,
        org_id=org_id,
    )
    return {"skills": skills}


@app.post("/api/v1/orgs/{org_id}/bots", responses={403: {"description": "Admin access required"}})
def register_byob_bot(org_id: str, payload: CreateByobBotRequest, user: CurrentUser) -> ByobBotSummary:
    return service.register_byob_bot(actor_user_id=int(user["user_id"]), org_id=org_id, payload=payload)


@app.get("/api/v1/orgs/{org_id}/bots", responses={403: {"description": "Admin access required"}})
def list_byob_bots(org_id: str, user: CurrentUser, provider: str | None = None) -> list[ByobBotSummary]:
    return service.list_byob_bots(actor_user_id=int(user["user_id"]), org_id=org_id, provider=provider)


@app.patch("/api/v1/orgs/{org_id}/bots/{bot_id}", responses={403: {"description": "Admin access required"}})
def update_byob_bot_status(
    org_id: str,
    bot_id: int,
    payload: UpdateByobBotStatusRequest,
    user: CurrentUser,
) -> ByobBotSummary:
    return service.update_byob_bot_status(
        actor_user_id=int(user["user_id"]),
        org_id=org_id,
        bot_id=bot_id,
        payload=payload,
    )


@app.get("/api/v1/orgs/{org_id}/credits/balance", responses={403: {"description": "Admin access required"}})
def get_org_credit_balance(org_id: str, user: CurrentUser) -> OrgCreditBalanceResponse:
    return service.get_org_credit_balance(actor_user_id=int(user["user_id"]), org_id=org_id)


@app.post("/api/v1/orgs/{org_id}/credits/topup", responses={403: {"description": "Admin access required"}})
def top_up_org_credits(org_id: str, payload: CreditAdjustmentRequest, user: CurrentUser) -> OrgCreditBalanceResponse:
    return service.top_up_org_credits(actor_user_id=int(user["user_id"]), org_id=org_id, payload=payload)


@app.post("/api/v1/orgs/{org_id}/credits/debit", responses={403: {"description": "Admin access required"}})
def debit_org_credits(org_id: str, payload: CreditAdjustmentRequest, user: CurrentUser) -> OrgCreditBalanceResponse:
    return service.debit_org_credits(actor_user_id=int(user["user_id"]), org_id=org_id, payload=payload)


@app.get("/api/v1/orgs/{org_id}/credits/ledger", responses={403: {"description": "Admin access required"}})
def list_org_credit_ledger(org_id: str, user: CurrentUser, limit: int = 50) -> CreditLedgerResponse:
    return service.list_org_credit_ledger(actor_user_id=int(user["user_id"]), org_id=org_id, limit=limit)


@app.get("/api/v1/orgs/{org_id}/credits/policy", responses={403: {"description": "Admin access required"}})
def get_org_credit_policy(org_id: str, user: CurrentUser) -> CreditPolicyResponse:
    return service.get_org_credit_policy(actor_user_id=int(user["user_id"]), org_id=org_id)


@app.put("/api/v1/orgs/{org_id}/credits/policy", responses={403: {"description": "Admin access required"}})
def set_org_credit_policy(org_id: str, payload: UpdateCreditPolicyRequest, user: CurrentUser) -> CreditPolicyResponse:
    return service.set_org_credit_policy(actor_user_id=int(user["user_id"]), org_id=org_id, payload=payload)


@app.post("/api/v1/byob/telegram/{org_id}/{bot_id}/webhook")
def byob_telegram_webhook(
    org_id: str,
    bot_id: int,
    request: Request,
    payload: TelegramWebhookUpdate,
    telegram_secret: Annotated[str | None, Header(alias="X-Telegram-Bot-Api-Secret-Token")] = None,
) -> dict[str, Any]:
    forwarded_for = str(request.headers.get("x-forwarded-for") or "").strip()
    client_ip = forwarded_for.split(",", 1)[0].strip() if forwarded_for else None
    if not client_ip:
        client_ip = request.client.host if request.client is not None else None
    return service.process_telegram_webhook(
        org_id=org_id,
        bot_id=bot_id,
        webhook_secret=str(telegram_secret or ""),
        client_ip=client_ip,
        update=payload,
    )


@app.get("/api/v1/orgs/{org_id}/queue/health", responses={403: {"description": "Admin access required"}})
def byob_queue_health(org_id: str, user: CurrentUser) -> ByobQueueHealthResponse:
    return service.get_byob_queue_health(actor_user_id=int(user["user_id"]), org_id=org_id)


@app.post("/api/v1/admin/byob/queue/process", responses={403: {"description": "Admin access required"}})
def process_byob_queue_now(user: CurrentUser) -> dict[str, int]:
    service._enforce_admin(int(user["user_id"]))
    processed = service.process_byob_delivery_backlog()
    return {"processed": int(processed)}


@app.get("/api/v1/admin/skills", responses={403: {"description": "Admin access required"}})
def list_dynamic_skills(user: CurrentUser) -> DynamicSkillListResponse:
    skills = service.list_dynamic_skills(actor_user_id=int(user["user_id"]))
    return DynamicSkillListResponse(skills=skills)


@app.post("/api/v1/admin/skills/convert-claude", responses={403: {"description": "Admin access required"}})
def convert_claude_skill(payload: ClaudeSkillConvertRequest, user: CurrentUser) -> dict[str, str]:
    return service.convert_claude_markdown_to_skill(actor_user_id=int(user["user_id"]), payload=payload)


@app.post("/api/v1/admin/skills/convert-claude-file", responses={403: {"description": "Admin access required"}})
async def convert_claude_skill_file(
    user: CurrentUser,
    file: UploadFile = File(...),
    skill_name: str | None = Form(default=None),
    overwrite: bool = Form(default=False),
) -> dict[str, str]:
    raw_bytes = await file.read()
    try:
        markdown = _decode_uploaded_markdown(raw_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    payload = ClaudeSkillConvertRequest(markdown=markdown, skill_name=skill_name, overwrite=overwrite)
    return service.convert_claude_markdown_to_skill(actor_user_id=int(user["user_id"]), payload=payload)


@app.post("/api/v1/admin/skills/convert-claude-files", responses={403: {"description": "Admin access required"}})
async def convert_claude_skill_files(
    user: CurrentUser,
    files: list[UploadFile] = File(...),
    skill_name_prefix: str | None = Form(default=None),
    overwrite: bool = Form(default=False),
) -> BulkClaudeConvertResponse:
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    prefix = (skill_name_prefix or "").strip()
    results: list[BulkClaudeConvertItem] = []
    created = 0
    failed = 0

    for item in files:
        filename = str(item.filename or "unnamed.md")
        try:
            raw = await item.read()
            markdown = _decode_uploaded_markdown(raw)

            explicit_name: str | None = None
            stem = Path(filename).stem.strip()
            if stem:
                explicit_name = f"{prefix}_{stem}" if prefix else stem

            payload = ClaudeSkillConvertRequest(
                markdown=markdown,
                skill_name=explicit_name,
                overwrite=overwrite,
            )
            converted = service.convert_claude_markdown_to_skill(
                actor_user_id=int(user["user_id"]),
                payload=payload,
            )
            created += 1
            results.append(
                BulkClaudeConvertItem(
                    filename=filename,
                    status="ok",
                    skill_name=str(converted.get("skill_name") or ""),
                )
            )
        except HTTPException as exc:
            failed += 1
            results.append(
                BulkClaudeConvertItem(
                    filename=filename,
                    status="error",
                    error=str(exc.detail),
                )
            )
        except Exception as exc:
            failed += 1
            results.append(
                BulkClaudeConvertItem(
                    filename=filename,
                    status="error",
                    error=str(exc),
                )
            )

    return BulkClaudeConvertResponse(created=created, failed=failed, results=results)


@app.post("/api/v1/admin/skills/convert-claude-files/dry-run", responses={403: {"description": "Admin access required"}})
async def convert_claude_skill_files_dry_run(
    user: CurrentUser,
    files: list[UploadFile] = File(...),
    skill_name_prefix: str | None = Form(default=None),
) -> BulkClaudeDryRunResponse:
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    prepared_files: list[tuple[str, str]] = []
    decode_errors: list[BulkClaudeDryRunItem] = []
    for item in files:
        filename = str(item.filename or "unnamed.md")
        try:
            raw = await item.read()
            markdown = _decode_uploaded_markdown(raw)
            prepared_files.append((filename, markdown))
        except ValueError as exc:
            decode_errors.append(
                BulkClaudeDryRunItem(
                    filename=filename,
                    status="error",
                    error=str(exc),
                )
            )

    preview = service.preview_bulk_claude_conversion(
        actor_user_id=int(user["user_id"]),
        files=prepared_files,
        skill_name_prefix=skill_name_prefix,
    )
    combined_results = [*decode_errors, *preview.results]
    valid = sum(1 for result in combined_results if result.status == "ok")
    invalid = len(combined_results) - valid
    return BulkClaudeDryRunResponse(
        total=len(combined_results),
        valid=valid,
        invalid=invalid,
        results=combined_results,
    )


@app.delete("/api/v1/admin/skills/{skill_name}", responses={403: {"description": "Admin access required"}})
def delete_dynamic_skill(skill_name: str, user: CurrentUser) -> DynamicSkillDeleteResponse:
    deleted = service.delete_dynamic_skill(actor_user_id=int(user["user_id"]), skill_name=skill_name)
    return DynamicSkillDeleteResponse(status="ok", deleted=deleted)


@app.post("/api/v1/chat/send")
async def chat_send(payload: ChatRequest, user: CurrentUser) -> ChatResponse:
    current_user_id = int(user["user_id"])
    response = service.send_group_chat(user_id=current_user_id, payload=payload)
    snapshot_payload = _build_chat_snapshot_payload(
        user_id=current_user_id,
        messages=response.messages,
    )
    await chat_hub.publish_snapshot(snapshot_payload)
    return response


@app.get("/api/v1/chat/messages")
def chat_messages(user: CurrentUser) -> list[ChatMessage]:
    return service._read_group_messages(user_id=int(user["user_id"]))


@app.websocket("/api/v1/chat/ws")
async def chat_websocket(websocket: WebSocket) -> None:
    auth_header = str(websocket.headers.get("authorization") or "").strip()
    query_token = str(websocket.query_params.get("token") or "").strip()
    if not auth_header and query_token:
        auth_header = f"Bearer {query_token}"

    try:
        token = _extract_bearer_token(auth_header)
        current_user = service.get_user_from_token(token)
    except HTTPException:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    await websocket.send_json({"type": "ready"})

    try:
        while True:
            incoming = await websocket.receive_json()
            action = str(incoming.get("action") or "").strip().lower()

            if action == "subscribe":
                current_user_id = int(current_user["user_id"])
                await chat_hub.register_scope(websocket, current_user_id)
                snapshot = _build_chat_snapshot_payload(
                    user_id=current_user_id,
                    messages=service._read_group_messages(user_id=current_user_id),
                )
                await websocket.send_json(snapshot)
                continue

            if action == "send":
                try:
                    request = WsSendMessageRequest.model_validate(incoming)
                except ValidationError as exc:
                    await websocket.send_json({"type": "error", "detail": str(exc)})
                    continue

                current_user_id = int(current_user["user_id"])

                try:
                    response = service.send_group_chat(
                        user_id=current_user_id,
                        payload=ChatRequest(
                            message=request.message,
                        ),
                    )
                except HTTPException:
                    await websocket.send_json({"type": "error", "detail": "forbidden"})
                    continue

                snapshot = _build_chat_snapshot_payload(
                    user_id=current_user_id,
                    messages=response.messages,
                    client_message_id=request.client_message_id,
                )
                await chat_hub.publish_snapshot(snapshot)
                continue

            await websocket.send_json({"type": "error", "detail": "unknown action"})
    except WebSocketDisconnect:
        pass
    finally:
        await chat_hub.unregister(websocket)
