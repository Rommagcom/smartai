from __future__ import annotations

import hashlib
import os
import secrets
import zlib
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
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

    def register_user(self, payload: RegisterRequest) -> int:
        password_hash = _hash_password(payload.password)
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            existing_admin = conn.execute(
                text(
                    """
                    SELECT 1
                    FROM app_users
                    WHERE org_id = :org_id AND role = 'admin'
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
        with self.engine.begin() as conn:
            row = conn.execute(
                text("SELECT user_id, password_hash FROM auth_users WHERE lower(email) = lower(:email) LIMIT 1"),
                {"email": email.strip()},
            ).mappings().first()
            if row is None or not _verify_password(password, str(row["password_hash"])):
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
        )

        user_profile = self._user_profile_text(user_id=user_id)
        history = self._load_group_history(org_id=org_id, team_id=team_id)
        memory_context = self._recall_shared_memory(org_id=org_id, team_id=team_id, user_id=user_id, query=payload.message)
        if user_profile:
            history = [{"role": "system", "content": user_profile}, *history]
        if memory_context:
            history = [{"role": "system", "content": memory_context}, *history]

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

        messages = self._read_group_messages(org_id=org_id, team_id=team_id)
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
app = FastAPI(title=APP_NAME)


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


@app.post("/api/v1/admin/organizations", responses={403: {"description": "Admin access required"}})
def create_org(payload: CreateOrgRequest, user: CurrentUser) -> dict[str, str]:
    service.create_org(actor_user_id=int(user["user_id"]), payload=payload)
    return {"status": "ok"}


@app.post("/api/v1/admin/teams", responses={403: {"description": "Admin access required"}})
def create_team(payload: CreateTeamRequest, user: CurrentUser) -> dict[str, str]:
    service.create_team(actor_user_id=int(user["user_id"]), payload=payload)
    return {"status": "ok"}


@app.post("/api/v1/admin/teams/members", responses={403: {"description": "Admin access required"}})
def add_to_team(payload: AddTeamMemberRequest, user: CurrentUser) -> dict[str, str]:
    service.add_to_team(actor_user_id=int(user["user_id"]), payload=payload)
    return {"status": "ok"}


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


@app.post("/api/v1/chat/send", responses={403: {"description": "User is not a member of the target team"}})
def chat_send(payload: ChatRequest, user: CurrentUser) -> ChatResponse:
    return service.send_group_chat(user_id=int(user["user_id"]), payload=payload)


@app.get("/api/v1/chat/messages", responses={403: {"description": "User is not a member of the target team"}})
def chat_messages(
    org_id: str,
    team_id: str,
    user: CurrentUser,
) -> list[ChatMessage]:
    service._assert_membership(org_id=org_id, team_id=team_id, user_id=int(user["user_id"]))
    return service._read_group_messages(org_id=org_id, team_id=team_id)
