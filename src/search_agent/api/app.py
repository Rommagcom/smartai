from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Sequence

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
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


def _team_chat_id(org_id: str, team_id: str) -> int:
    key = f"{org_id}:{team_id}"
    return int(zlib.crc32(key.encode("utf-8")) & 0x7FFFFFFF)


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


class CreateTeamRequest(BaseModel):
    org_id: str = Field(min_length=1, max_length=128)
    team_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)


class AddTeamMemberRequest(BaseModel):
    org_id: str = Field(min_length=1, max_length=128)
    team_id: str = Field(min_length=1, max_length=128)
    user_id: int


class SetRoleRequest(BaseModel):
    org_id: str = Field(min_length=1, max_length=128)
    role: Role


class SkillAssignmentRequest(BaseModel):
    org_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=128)


class TeamSkillAssignmentRequest(BaseModel):
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


class ChatRequest(BaseModel):
    org_id: str = Field(min_length=1, max_length=128)
    team_id: str = Field(min_length=1, max_length=128)
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
    org_id: str = Field(min_length=1, max_length=128)
    team_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=8000)
    client_message_id: str | None = Field(default=None, max_length=128)


class TeamSummary(BaseModel):
    org_id: str
    team_id: str
    team_name: str


class OrganizationSummary(BaseModel):
    org_id: str
    name: str


class TeamMemberSummary(BaseModel):
    user_id: int
    email: str
    full_name: str
    title: str
    profile_bio: str
    role: str


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


class RealtimeChatHub:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = asyncio.Lock()
        self._team_sockets: dict[str, set[WebSocket]] = {}
        self._socket_team: dict[WebSocket, str] = {}
        self._redis: redis_async.Redis | None = None
        self._pubsub_task: asyncio.Task[Any] | None = None
        self._broadcast_channel = f"{settings.redis_key_prefix}:ws:broadcast"

    @staticmethod
    def _team_key(org_id: str, team_id: str) -> str:
        return f"{org_id}:{team_id}"

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

    async def register_team(self, websocket: WebSocket, org_id: str, team_id: str) -> None:
        team_key = self._team_key(org_id, team_id)
        async with self._lock:
            previous = self._socket_team.get(websocket)
            if previous and previous in self._team_sockets:
                self._team_sockets[previous].discard(websocket)
                if not self._team_sockets[previous]:
                    del self._team_sockets[previous]
            self._socket_team[websocket] = team_key
            self._team_sockets.setdefault(team_key, set()).add(websocket)

    async def unregister(self, websocket: WebSocket) -> None:
        async with self._lock:
            team_key = self._socket_team.pop(websocket, None)
            if team_key and team_key in self._team_sockets:
                self._team_sockets[team_key].discard(websocket)
                if not self._team_sockets[team_key]:
                    del self._team_sockets[team_key]

    async def publish_snapshot(self, snapshot_payload: dict[str, Any]) -> None:
        if self._redis is not None:
            await self._redis.publish(self._broadcast_channel, json.dumps(snapshot_payload, ensure_ascii=True))
        await self._broadcast_local(snapshot_payload)

    async def _broadcast_local(self, snapshot_payload: dict[str, Any]) -> None:
        org_id = str(snapshot_payload.get("org_id") or "").strip()
        team_id = str(snapshot_payload.get("team_id") or "").strip()
        if not org_id or not team_id:
            return
        team_key = self._team_key(org_id, team_id)
        async with self._lock:
            sockets = list(self._team_sockets.get(team_key, set()))

        dropped: list[WebSocket] = []
        for socket in sockets:
            try:
                await socket.send_json(snapshot_payload)
            except Exception:
                dropped.append(socket)

        if dropped:
            async with self._lock:
                for socket in dropped:
                    old_key = self._socket_team.pop(socket, None)
                    if old_key and old_key in self._team_sockets:
                        self._team_sockets[old_key].discard(socket)
                        if not self._team_sockets[old_key]:
                            del self._team_sockets[old_key]

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
                {"org_id": self.settings.tenant_default_org_id},
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
            self.rbac.ensure_organization(org_id=self.settings.tenant_default_org_id, name=self.settings.tenant_default_org_id)
            role: Role = "admin" if existing_admin is None else "member"
            self.rbac.upsert_user(org_id=self.settings.tenant_default_org_id, user_id=user_id, role=role)
            conn.execute(
                text(
                    """
                    INSERT INTO org_memberships (org_id, user_id, created_at)
                    VALUES (:org_id, :user_id, :created_at)
                    ON CONFLICT (org_id, user_id) DO NOTHING
                    """
                ),
                {
                    "org_id": self.settings.tenant_default_org_id,
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

    def list_user_teams(self, *, user_id: int, org_id: str | None = None) -> list[TeamSummary]:
        filter_org = (org_id or "").strip()
        where_clause = "WHERE tm.user_id = :user_id"
        params: dict[str, Any] = {"user_id": int(user_id)}
        if filter_org:
            where_clause += " AND tm.org_id = :org_id"
            params["org_id"] = filter_org

        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT
                        tm.org_id,
                        tm.team_id,
                        COALESCE(t.name, tm.team_id) AS team_name
                    FROM team_members tm
                    LEFT JOIN teams t
                      ON t.org_id = tm.org_id AND t.team_id = tm.team_id
                    {where_clause}
                    ORDER BY tm.org_id ASC, tm.team_id ASC
                    """
                ),
                params,
            ).mappings().all()

        return [
            TeamSummary(
                org_id=str(row.get("org_id") or ""),
                team_id=str(row.get("team_id") or ""),
                team_name=str(row.get("team_name") or row.get("team_id") or ""),
            )
            for row in rows
            if str(row.get("org_id") or "").strip() and str(row.get("team_id") or "").strip()
        ]

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

    def list_org_teams(self, *, actor_user_id: int, org_id: str) -> list[TeamSummary]:
        self._enforce_admin(actor_user_id)
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT org_id, team_id, name
                    FROM teams
                    WHERE org_id = :org_id
                    ORDER BY team_id ASC
                    """
                ),
                {"org_id": org_id},
            ).mappings().all()
        return [
            TeamSummary(
                org_id=str(row.get("org_id") or org_id),
                team_id=str(row.get("team_id") or ""),
                team_name=str(row.get("name") or row.get("team_id") or ""),
            )
            for row in rows
            if str(row.get("team_id") or "").strip()
        ]

    def list_team_members(self, *, actor_user_id: int, org_id: str, team_id: str) -> list[TeamMemberSummary]:
        self._enforce_admin(actor_user_id)
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
                        COALESCE(au.role, 'member') AS role
                    FROM team_members tm
                    JOIN auth_users u
                      ON u.user_id = tm.user_id
                    LEFT JOIN app_users au
                      ON au.org_id = tm.org_id AND au.user_id = tm.user_id
                    WHERE tm.org_id = :org_id AND tm.team_id = :team_id
                    ORDER BY u.user_id ASC
                    """
                ),
                {
                    "org_id": org_id,
                    "team_id": team_id,
                },
            ).mappings().all()
        return [
            TeamMemberSummary(
                user_id=int(row.get("user_id") or 0),
                email=str(row.get("email") or ""),
                full_name=str(row.get("full_name") or ""),
                title=str(row.get("title") or ""),
                profile_bio=str(row.get("profile_bio") or ""),
                role=str(row.get("role") or "member"),
            )
            for row in rows
            if int(row.get("user_id") or 0) > 0
        ]

    def remove_from_team(self, *, actor_user_id: int, org_id: str, team_id: str, target_user_id: int) -> bool:
        self._enforce_admin(actor_user_id)
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    DELETE FROM team_members
                    WHERE org_id = :org_id AND team_id = :team_id AND user_id = :user_id
                    """
                ),
                {
                    "org_id": org_id,
                    "team_id": team_id,
                    "user_id": int(target_user_id),
                },
            )
        removed = int(result.rowcount or 0) > 0
        self.rbac.audit(
            org_id=org_id,
            team_id=team_id,
            actor_user_id=actor_user_id,
            action="rbac.remove_user_from_team",
            target_type="team_member",
            target_id=f"{team_id}:{int(target_user_id)}",
            details={"removed": removed},
        )
        return removed

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
            org_id=self.settings.tenant_default_org_id,
            user_id=int(user_id),
            fallback_role="member",
        )
        return role == "admin"

    def _enforce_admin(self, user_id: int) -> None:
        if not self._is_global_admin(user_id):
            raise HTTPException(status_code=403, detail="Admin access required")

    def create_org(self, *, actor_user_id: int, payload: CreateOrgRequest) -> None:
        self._enforce_admin(actor_user_id)
        self.rbac.create_organization(actor_user_id=actor_user_id, org_id=payload.org_id, name=payload.name)

    def create_team(self, *, actor_user_id: int, payload: CreateTeamRequest) -> None:
        self._enforce_admin(actor_user_id)
        self.rbac.create_team(
            actor_user_id=actor_user_id,
            org_id=payload.org_id,
            team_id=payload.team_id,
            name=payload.name,
        )

    def add_to_team(self, *, actor_user_id: int, payload: AddTeamMemberRequest) -> None:
        self._enforce_admin(actor_user_id)
        self.rbac.add_user_to_team(
            actor_user_id=actor_user_id,
            org_id=payload.org_id,
            team_id=payload.team_id,
            user_id=payload.user_id,
        )
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
                    "user_id": int(payload.user_id),
                    "created_at": datetime.now(UTC),
                },
            )

    def set_role(self, *, actor_user_id: int, target_user_id: int, payload: SetRoleRequest) -> None:
        self._enforce_admin(actor_user_id)
        self.rbac.set_role(
            org_id=payload.org_id,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            role=payload.role,
        )

    def list_org_users(self, *, actor_user_id: int, org_id: str, team_id: str | None = None) -> list[AdminUserSummary]:
        self._enforce_admin(actor_user_id)
        normalized_team_id = (team_id or "").strip()
        supports_otp = self._supports_auth_otp_fields()
        team_join = (
            "LEFT JOIN team_members tm ON tm.org_id = om.org_id AND tm.team_id = :team_id AND tm.user_id = om.user_id"
            if normalized_team_id
            else "LEFT JOIN team_members tm ON 1 = 0"
        )
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
                        CASE
                            WHEN tm.user_id IS NULL THEN FALSE
                            ELSE TRUE
                        END AS in_team
                    FROM org_memberships om
                    JOIN auth_users u
                      ON u.user_id = om.user_id
                    LEFT JOIN app_users au
                      ON au.org_id = om.org_id AND au.user_id = om.user_id
                                        """
                                        + team_join
                                        +
                                        """
                    WHERE om.org_id = :org_id
                    ORDER BY u.user_id ASC
                    """
                ),
                {
                    "org_id": org_id,
                                        "team_id": normalized_team_id,
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
        self._enforce_admin(actor_user_id)
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
        self._enforce_admin(actor_user_id)
        normalized_org = org_id.strip()
        if not normalized_org:
            raise HTTPException(status_code=400, detail="Organization ID is required")

        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    DELETE FROM team_members
                    WHERE org_id = :org_id AND user_id = :user_id
                    """
                ),
                {
                    "org_id": normalized_org,
                    "user_id": int(target_user_id),
                },
            )
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
        self._enforce_admin(actor_user_id)
        self.rbac.assign_skill(
            org_id=payload.org_id,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            tool_name=payload.tool_name,
        )

    def revoke_skill(self, *, actor_user_id: int, target_user_id: int, payload: SkillAssignmentRequest) -> bool:
        self._enforce_admin(actor_user_id)
        return self.rbac.revoke_skill(
            org_id=payload.org_id,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            tool_name=payload.tool_name,
        )

    def list_user_skills(self, *, actor_user_id: int, target_user_id: int, org_id: str) -> list[str]:
        self._enforce_admin(actor_user_id)
        return self.rbac.list_user_skills(org_id=org_id, user_id=target_user_id)

    def list_team_skills(self, *, actor_user_id: int, org_id: str, team_id: str) -> list[str]:
        self._enforce_admin(actor_user_id)
        return self.rbac.list_team_skills(org_id=org_id, team_id=team_id)

    def grant_team_skill(self, *, actor_user_id: int, team_id: str, payload: TeamSkillAssignmentRequest) -> None:
        self._enforce_admin(actor_user_id)
        self.rbac.assign_team_skill(
            org_id=payload.org_id,
            team_id=team_id,
            actor_user_id=actor_user_id,
            tool_name=payload.tool_name,
        )

    def revoke_team_skill(self, *, actor_user_id: int, team_id: str, payload: TeamSkillAssignmentRequest) -> bool:
        self._enforce_admin(actor_user_id)
        return self.rbac.revoke_team_skill(
            org_id=payload.org_id,
            team_id=team_id,
            actor_user_id=actor_user_id,
            tool_name=payload.tool_name,
        )

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

    def _load_group_history(self, *, org_id: str, team_id: str, limit: int = GROUP_SHORT_MEMORY_LIMIT) -> list[dict[str, str]]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT sender_type, content
                    FROM group_messages
                    WHERE org_id = :org_id AND team_id = :team_id
                    ORDER BY created_at DESC
                    LIMIT :limit
                    """
                ),
                {
                    "org_id": org_id,
                    "team_id": team_id,
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
        org_id: str,
        team_id: str,
        sender_user_id: int | None,
        sender_type: str,
        content: str,
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO group_messages (org_id, team_id, sender_user_id, sender_type, content, created_at)
                    VALUES (:org_id, :team_id, :sender_user_id, :sender_type, :content, :created_at)
                    """
                ),
                {
                    "org_id": org_id,
                    "team_id": team_id,
                    "sender_user_id": int(sender_user_id) if sender_user_id is not None else None,
                    "sender_type": sender_type,
                    "content": content,
                    "created_at": datetime.now(UTC),
                },
            )

    def _read_group_messages(self, *, org_id: str, team_id: str, limit: int = 30) -> list[ChatMessage]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT sender_type, sender_user_id, content, created_at::text AS created_at
                    FROM group_messages
                    WHERE org_id = :org_id AND team_id = :team_id
                    ORDER BY created_at DESC
                    LIMIT :limit
                    """
                ),
                {
                    "org_id": org_id,
                    "team_id": team_id,
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

    def _recall_shared_memory(self, *, org_id: str, team_id: str, user_id: int, query: str) -> str:
        if self.long_term is None:
            return ""

        combined: list[str] = []
        try:
            shared = self.long_term.recall(
                org_id=org_id,
                team_id=team_id,
                user_id=0,
                chat_id=_team_chat_id(org_id, team_id),
                query_text=query,
                limit=3,
            )
            personal = self.long_term.recall(
                org_id=org_id,
                team_id=team_id,
                user_id=user_id,
                chat_id=_team_chat_id(org_id, team_id),
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
        org_id = payload.org_id.strip()
        team_id = payload.team_id.strip()
        self._assert_membership(org_id=org_id, team_id=team_id, user_id=user_id)

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
            team_id=team_id,
        )

        user_profile = self._user_profile_text(user_id=user_id)
        team_roster_profile = self._team_members_context(org_id=org_id, team_id=team_id)
        try:
            history = self._load_group_history(org_id=org_id, team_id=team_id)
        except Exception:
            history = []
        memory_context = self._recall_shared_memory(org_id=org_id, team_id=team_id, user_id=user_id, query=payload.message)
        if user_profile:
            history = [{"role": "system", "content": user_profile}, *history]
        if team_roster_profile:
            history = [{"role": "system", "content": team_roster_profile}, *history]
        if memory_context:
            history = [{"role": "system", "content": memory_context}, *history]

        try:
            answer_result = self.agent.run(
                payload.message,
                history,
                _team_chat_id(org_id, team_id),
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
                org_id=org_id,
                team_id=team_id,
                sender_user_id=user_id,
                sender_type="user",
                content=payload.message,
            )
            self._append_group_message(
                org_id=org_id,
                team_id=team_id,
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
                    team_id=team_id,
                    user_id=user_id,
                    chat_id=_team_chat_id(org_id, team_id),
                    user_text=payload.message,
                    assistant_text=answer,
                    source="api-chat",
                )
                self.long_term.remember(
                    org_id=org_id,
                    team_id=team_id,
                    user_id=0,
                    chat_id=_team_chat_id(org_id, team_id),
                    user_text=payload.message,
                    assistant_text=answer,
                    source="api-group",
                )
            except Exception:
                pass

        try:
            messages = self._read_group_messages(org_id=org_id, team_id=team_id)
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

    def _team_members_context(self, *, org_id: str, team_id: str) -> str:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT u.full_name, u.title, u.profile_bio
                    FROM team_members tm
                    JOIN auth_users u ON u.user_id = tm.user_id
                    WHERE tm.org_id = :org_id AND tm.team_id = :team_id
                    ORDER BY u.full_name ASC, u.user_id ASC
                    LIMIT 30
                    """
                ),
                {
                    "org_id": org_id,
                    "team_id": team_id,
                },
            ).mappings().all()

        if not rows:
            return ""

        lines = ["Current team roster context:"]
        for idx, row in enumerate(rows, start=1):
            full_name = str(row.get("full_name") or "Unknown").strip() or "Unknown"
            title = str(row.get("title") or "").strip() or "Unknown"
            bio = str(row.get("profile_bio") or "").strip() or "Not provided"
            lines.append(f"{idx}. {full_name} | {title} | {bio}")
        return "\n".join(lines)


settings = load_settings()
service = ApiService(settings)
app = FastAPI(title=APP_NAME)
chat_hub = RealtimeChatHub(settings)


@app.on_event("startup")
async def startup_event() -> None:
    await chat_hub.startup()


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await chat_hub.shutdown()


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


def _build_chat_snapshot_payload(
    *,
    org_id: str,
    team_id: str,
    messages: Sequence[ChatMessage],
    client_message_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "chat.snapshot",
        "org_id": org_id,
        "team_id": team_id,
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


@app.get("/api/v1/users/me/teams")
def list_my_teams(
    user: CurrentUser,
    org_id: str | None = None,
) -> list[TeamSummary]:
    return service.list_user_teams(user_id=int(user["user_id"]), org_id=org_id)


@app.post("/api/v1/admin/organizations", responses={403: {"description": "Admin access required"}})
def create_org(payload: CreateOrgRequest, user: CurrentUser) -> dict[str, str]:
    service.create_org(actor_user_id=int(user["user_id"]), payload=payload)
    return {"status": "ok"}


@app.get("/api/v1/admin/organizations", responses={403: {"description": "Admin access required"}})
def list_organizations(user: CurrentUser) -> list[OrganizationSummary]:
    return service.list_organizations(actor_user_id=int(user["user_id"]))


@app.post("/api/v1/admin/teams", responses={403: {"description": "Admin access required"}})
def create_team(payload: CreateTeamRequest, user: CurrentUser) -> dict[str, str]:
    service.create_team(actor_user_id=int(user["user_id"]), payload=payload)
    return {"status": "ok"}


@app.get("/api/v1/admin/organizations/{org_id}/teams", responses={403: {"description": "Admin access required"}})
def list_org_teams(org_id: str, user: CurrentUser) -> list[TeamSummary]:
    return service.list_org_teams(actor_user_id=int(user["user_id"]), org_id=org_id)


@app.post("/api/v1/admin/teams/members", responses={403: {"description": "Admin access required"}})
def add_to_team(payload: AddTeamMemberRequest, user: CurrentUser) -> dict[str, str]:
    service.add_to_team(actor_user_id=int(user["user_id"]), payload=payload)
    return {"status": "ok"}


@app.get("/api/v1/admin/teams/{team_id}/members", responses={403: {"description": "Admin access required"}})
def list_team_members(team_id: str, org_id: str, user: CurrentUser) -> list[TeamMemberSummary]:
    return service.list_team_members(actor_user_id=int(user["user_id"]), org_id=org_id, team_id=team_id)


@app.delete("/api/v1/admin/teams/{team_id}/members/{target_user_id}", responses={403: {"description": "Admin access required"}})
def remove_from_team(team_id: str, target_user_id: int, org_id: str, user: CurrentUser) -> dict[str, Any]:
    removed = service.remove_from_team(
        actor_user_id=int(user["user_id"]),
        org_id=org_id,
        team_id=team_id,
        target_user_id=target_user_id,
    )
    return {"status": "ok", "removed": removed}


@app.get("/api/v1/admin/users", responses={403: {"description": "Admin access required"}})
def list_org_users(
    org_id: str,
    user: CurrentUser,
    team_id: str | None = None,
) -> list[AdminUserSummary]:
    return service.list_org_users(actor_user_id=int(user["user_id"]), org_id=org_id, team_id=team_id)


@app.get("/api/v1/admin/users/all", responses={403: {"description": "Admin access required"}})
def list_all_users(user: CurrentUser) -> list[GlobalAdminUserSummary]:
    return service.list_all_users(actor_user_id=int(user["user_id"]))


@app.post("/api/v1/admin/users", responses={403: {"description": "Admin access required"}, 409: {"description": "Email already registered"}})
def create_user_by_admin(payload: AdminCreateUserRequest, user: CurrentUser) -> dict[str, Any]:
    return service.create_user_by_admin(actor_user_id=int(user["user_id"]), payload=payload)


@app.patch("/api/v1/admin/users/{target_user_id}", responses={403: {"description": "Admin access required"}})
def update_user_by_admin(target_user_id: int, payload: AdminUpdateUserRequest, user: CurrentUser) -> dict[str, str]:
    service.update_user_by_admin(actor_user_id=int(user["user_id"]), target_user_id=target_user_id, payload=payload)
    return {"status": "ok"}


@app.post("/api/v1/admin/users/{target_user_id}/otp", responses={403: {"description": "Admin access required"}})
def reset_user_otp(target_user_id: int, payload: ResetPasswordRequest, user: CurrentUser) -> dict[str, Any]:
    return service.reset_user_password_one_time(
        actor_user_id=int(user["user_id"]),
        org_id=payload.org_id,
        target_user_id=target_user_id,
        ttl_minutes=payload.ttl_minutes,
    )


@app.post("/api/v1/admin/users/{target_user_id}/force-password-change", responses={403: {"description": "Admin access required"}})
def force_user_password_change(target_user_id: int, payload: ForcePasswordChangeRequest, user: CurrentUser) -> dict[str, Any]:
    return service.force_user_password_change(
        actor_user_id=int(user["user_id"]),
        org_id=payload.org_id,
        target_user_id=target_user_id,
    )


@app.post("/api/v1/admin/users/{target_user_id}/organizations/add", responses={403: {"description": "Admin access required"}})
def bind_user_to_org(target_user_id: int, payload: MembershipUpsertRequest, user: CurrentUser) -> dict[str, Any]:
    return service.bind_user_to_org(
        actor_user_id=int(user["user_id"]),
        target_user_id=target_user_id,
        payload=payload,
    )


@app.delete("/api/v1/admin/users/{target_user_id}/organizations/{org_id}", responses={403: {"description": "Admin access required"}})
def unbind_user_from_org(target_user_id: int, org_id: str, user: CurrentUser) -> dict[str, Any]:
    return service.unbind_user_from_org(
        actor_user_id=int(user["user_id"]),
        target_user_id=target_user_id,
        org_id=org_id,
    )


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
    skills = service.list_user_skills(actor_user_id=int(user["user_id"]), target_user_id=target_user_id, org_id=org_id)
    return {"skills": skills}


@app.get("/api/v1/admin/teams/{team_id}/skills", responses={403: {"description": "Admin access required"}})
def list_team_skills(
    team_id: str,
    org_id: str,
    user: CurrentUser,
) -> dict[str, Any]:
    skills = service.list_team_skills(actor_user_id=int(user["user_id"]), org_id=org_id, team_id=team_id)
    return {"skills": skills}


@app.post(
    "/api/v1/admin/teams/{team_id}/skills/grant",
    responses={403: {"description": "Admin access required"}},
)
def grant_team_skill(
    team_id: str,
    payload: TeamSkillAssignmentRequest,
    user: CurrentUser,
) -> dict[str, str]:
    service.grant_team_skill(actor_user_id=int(user["user_id"]), team_id=team_id, payload=payload)
    return {"status": "ok"}


@app.post(
    "/api/v1/admin/teams/{team_id}/skills/revoke",
    responses={403: {"description": "Admin access required"}},
)
def revoke_team_skill(
    team_id: str,
    payload: TeamSkillAssignmentRequest,
    user: CurrentUser,
) -> dict[str, Any]:
    removed = service.revoke_team_skill(actor_user_id=int(user["user_id"]), team_id=team_id, payload=payload)
    return {"status": "ok", "removed": removed}


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


@app.post("/api/v1/chat/send", responses={403: {"description": "User is not a member of the target team"}})
async def chat_send(payload: ChatRequest, user: CurrentUser) -> ChatResponse:
    response = service.send_group_chat(user_id=int(user["user_id"]), payload=payload)
    snapshot_payload = _build_chat_snapshot_payload(
        org_id=payload.org_id,
        team_id=payload.team_id,
        messages=response.messages,
    )
    await chat_hub.publish_snapshot(snapshot_payload)
    return response


@app.get("/api/v1/chat/messages", responses={403: {"description": "User is not a member of the target team"}})
def chat_messages(
    org_id: str,
    team_id: str,
    user: CurrentUser,
) -> list[ChatMessage]:
    service._assert_membership(org_id=org_id, team_id=team_id, user_id=int(user["user_id"]))
    return service._read_group_messages(org_id=org_id, team_id=team_id)


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
                org_id = str(incoming.get("org_id") or "").strip()
                team_id = str(incoming.get("team_id") or "").strip()
                if not org_id or not team_id:
                    await websocket.send_json({"type": "error", "detail": "org_id and team_id are required"})
                    continue

                try:
                    service._assert_membership(
                        org_id=org_id,
                        team_id=team_id,
                        user_id=int(current_user["user_id"]),
                    )
                except HTTPException:
                    await websocket.send_json({"type": "error", "detail": "forbidden"})
                    continue

                await chat_hub.register_team(websocket, org_id, team_id)
                snapshot = _build_chat_snapshot_payload(
                    org_id=org_id,
                    team_id=team_id,
                    messages=service._read_group_messages(org_id=org_id, team_id=team_id),
                )
                await websocket.send_json(snapshot)
                continue

            if action == "send":
                try:
                    request = WsSendMessageRequest.model_validate(incoming)
                except ValidationError as exc:
                    await websocket.send_json({"type": "error", "detail": str(exc)})
                    continue

                try:
                    response = service.send_group_chat(
                        user_id=int(current_user["user_id"]),
                        payload=ChatRequest(
                            org_id=request.org_id,
                            team_id=request.team_id,
                            message=request.message,
                        ),
                    )
                except HTTPException:
                    await websocket.send_json({"type": "error", "detail": "forbidden"})
                    continue

                snapshot = _build_chat_snapshot_payload(
                    org_id=request.org_id,
                    team_id=request.team_id,
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
