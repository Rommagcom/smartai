from __future__ import annotations

import os
import importlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

os.environ.setdefault("REMINDER_DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
os.environ.setdefault("ENABLE_LONG_TERM_MEMORY", "0")
os.environ.setdefault("ENABLE_DYNAMIC_TOOLS", "0")

api_app = importlib.import_module("search_agent.api.app")


class _FakeApiService:
    def __init__(self) -> None:
        self._next_user_id = 1
        self.users: dict[int, dict[str, Any]] = {}
        self.users_by_email: dict[str, int] = {}
        self.tokens: dict[str, int] = {}

        self.organizations: dict[str, str] = {"acme": "Acme"}
        self.memberships: set[tuple[str, int]] = set()
        self.roles: dict[tuple[str, int], str] = {}
        self.skills: dict[tuple[str, int], set[str]] = {}
        self.messages: dict[int, list[dict[str, Any]]] = {}
        self.dynamic_skills: dict[str, dict[str, str]] = {}
        self.chat_sessions: dict[int, dict[str, dict[str, Any]]] = {}

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).isoformat()

    def _ensure_user_session_store(self, user_id: int) -> dict[str, dict[str, Any]]:
        return self.chat_sessions.setdefault(int(user_id), {})

    def register_user(self, payload: Any) -> int:
        email = payload.email.strip().lower()
        if email in self.users_by_email:
            raise HTTPException(status_code=409, detail="Email already registered")

        user_id = self._next_user_id
        self._next_user_id += 1
        self.users[user_id] = {
            "user_id": user_id,
            "email": email,
            "password": payload.password,
            "full_name": payload.full_name,
            "title": payload.title,
            "profile_bio": payload.profile_bio,
            "telegram_id": None,
        }
        self.users_by_email[email] = user_id
        return user_id

    def create_token(self, *, email: str, password: str) -> Any:
        user_id = self.users_by_email.get(email.strip().lower())
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        if self.users[user_id]["password"] != password:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        token = f"token-{user_id}-{len(self.tokens) + 1}"
        self.tokens[token] = user_id
        expires_at = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
        return api_app.TokenResponse(access_token=token, expires_at=expires_at)

    def get_user_from_token(self, token: str) -> dict[str, Any]:
        user_id = self.tokens.get(token)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        return dict(self.users[user_id])

    def link_telegram(self, *, user_id: int, telegram_id: int) -> None:
        if user_id not in self.users:
            raise HTTPException(status_code=401, detail="Invalid token")
        self.users[user_id]["telegram_id"] = int(telegram_id)

    def _ensure_admin(self, actor_user_id: int) -> None:
        role = self.roles.get(("acme", actor_user_id), "member")
        if role != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")

    def create_org(self, *, actor_user_id: int, payload: Any) -> None:
        self._ensure_admin(actor_user_id)
        org_id = str(payload.org_id).strip()
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID is required")
        self.organizations[org_id] = str(payload.name).strip() or org_id

    def list_organizations(self, *, actor_user_id: int) -> list[Any]:
        self._ensure_admin(actor_user_id)
        return [
            api_app.OrganizationSummary(org_id=org_id, name=name)
            for org_id, name in sorted(self.organizations.items())
        ]

    def bind_user_to_org(self, *, actor_user_id: int, target_user_id: int, payload: Any) -> dict[str, Any]:
        self._ensure_admin(actor_user_id)
        org_id = str(payload.org_id).strip()
        if org_id not in self.organizations:
            raise HTTPException(status_code=404, detail="Organization not found")
        if int(target_user_id) not in self.users:
            raise HTTPException(status_code=404, detail="User not found")

        key = (org_id, int(target_user_id))
        created = key not in self.memberships
        self.memberships.add(key)
        self.roles[key] = str(payload.role)
        return {"status": "ok", "created": created}

    def unbind_user_from_org(self, *, actor_user_id: int, target_user_id: int, org_id: str) -> dict[str, Any]:
        self._ensure_admin(actor_user_id)
        key = (str(org_id).strip(), int(target_user_id))
        removed = key in self.memberships
        if removed:
            self.memberships.remove(key)
        self.roles.pop(key, None)
        self.skills.pop(key, None)
        return {"status": "ok", "removed": removed}

    def delete_user_by_admin(self, *, actor_user_id: int, target_user_id: int) -> dict[str, Any]:
        self._ensure_admin(actor_user_id)
        normalized_id = int(target_user_id)
        if normalized_id <= 0:
            raise HTTPException(status_code=400, detail="Invalid target user id")
        if normalized_id == int(actor_user_id):
            raise HTTPException(status_code=400, detail="Cannot delete current admin user")
        if normalized_id not in self.users:
            raise HTTPException(status_code=404, detail="User not found")

        email = str(self.users[normalized_id]["email"])
        del self.users[normalized_id]
        self.users_by_email.pop(email, None)
        self.messages.pop(normalized_id, None)

        self.tokens = {token: uid for token, uid in self.tokens.items() if uid != normalized_id}
        self.memberships = {item for item in self.memberships if item[1] != normalized_id}
        self.roles = {key: role for key, role in self.roles.items() if key[1] != normalized_id}
        self.skills = {key: value for key, value in self.skills.items() if key[1] != normalized_id}
        return {"status": "ok", "deleted": True, "user_id": normalized_id}

    def set_role(self, *, actor_user_id: int, target_user_id: int, payload: Any) -> None:
        self._ensure_admin(actor_user_id)
        self.roles[(payload.org_id, int(target_user_id))] = payload.role

    def grant_skill(self, *, actor_user_id: int, target_user_id: int, payload: Any) -> None:
        self._ensure_admin(actor_user_id)
        key = (payload.org_id, int(target_user_id))
        current = self.skills.setdefault(key, set())
        current.add(payload.tool_name)

    def revoke_skill(self, *, actor_user_id: int, target_user_id: int, payload: Any) -> bool:
        self._ensure_admin(actor_user_id)
        key = (payload.org_id, int(target_user_id))
        current = self.skills.setdefault(key, set())
        if payload.tool_name in current:
            current.remove(payload.tool_name)
            return True
        return False

    def list_user_skills(self, *, actor_user_id: int, target_user_id: int, org_id: str) -> list[str]:
        self._ensure_admin(actor_user_id)
        return sorted(self.skills.get((org_id, int(target_user_id)), set()))

    def list_dynamic_skills(self, *, actor_user_id: int) -> list[Any]:
        self._ensure_admin(actor_user_id)
        return [
            api_app.DynamicSkillSummary(
                folder=record["folder"],
                tool_name=tool_name,
                description=record["description"],
            )
            for tool_name, record in sorted(self.dynamic_skills.items())
        ]

    def convert_claude_markdown_to_skill(self, *, actor_user_id: int, payload: Any) -> dict[str, str]:
        self._ensure_admin(actor_user_id)
        provided_name = (payload.skill_name or "").strip()
        markdown = str(payload.markdown)
        base_name = provided_name or "generated_skill"
        tool_name = base_name.lower().replace("-", "_").replace(" ", "_")
        tool_name = "".join(char if (char.isalnum() or char == "_") else "_" for char in tool_name).strip("_")
        if not tool_name:
            tool_name = "generated_skill"

        if tool_name in self.dynamic_skills and not bool(payload.overwrite):
            raise HTTPException(status_code=409, detail=f"Skill '{tool_name}' already exists. Use overwrite=true to replace.")

        self.dynamic_skills[tool_name] = {
            "folder": tool_name,
            "description": "Generated from test payload",
            "source": markdown,
        }
        return {
            "status": "ok",
            "skill_name": tool_name,
            "skill_dir": f"skills/{tool_name}",
        }

    def delete_dynamic_skill(self, *, actor_user_id: int, skill_name: str) -> bool:
        self._ensure_admin(actor_user_id)
        normalized = skill_name.lower().replace("-", "_").replace(" ", "_")
        if normalized in self.dynamic_skills:
            del self.dynamic_skills[normalized]
            return True
        return False

    def preview_bulk_claude_conversion(
        self,
        *,
        actor_user_id: int,
        files: list[tuple[str, str]],
        skill_name_prefix: str | None,
    ) -> Any:
        self._ensure_admin(actor_user_id)
        prefix = (skill_name_prefix or "").strip().lower().replace("-", "_").replace(" ", "_")
        results: list[Any] = []
        valid = 0
        invalid = 0
        for filename, markdown in files:
            if "---" not in markdown and "#" not in markdown:
                invalid += 1
                results.append(api_app.BulkClaudeDryRunItem(filename=filename, status="error", error="Invalid markdown"))
                continue

            stem = Path(filename).stem.lower().replace("-", "_").replace(" ", "_")
            proposed = f"{prefix}_{stem}" if prefix else stem
            exists = proposed in self.dynamic_skills
            valid += 1
            results.append(
                api_app.BulkClaudeDryRunItem(
                    filename=filename,
                    status="ok",
                    proposed_skill_name=proposed,
                    exists=exists,
                )
            )

        return api_app.BulkClaudeDryRunResponse(total=len(files), valid=valid, invalid=invalid, results=results)

    def _read_group_messages(
        self,
        *,
        user_id: int,
        chat_id: str | None = None,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        history = self.messages.get(int(user_id), [])
        return history[-max(1, int(limit)) :]

    def send_group_chat(self, *, user_id: int, payload: Any) -> Any:
        history = self.messages.setdefault(int(user_id), [])
        now = self._now_iso()
        history.append(
            api_app.ChatMessage(
                sender_type="user",
                sender_user_id=int(user_id),
                content=payload.message,
                created_at=now,
            )
        )
        answer = f"ACK: {payload.message}"
        history.append(
            api_app.ChatMessage(
                sender_type="assistant",
                sender_user_id=None,
                content=answer,
                created_at=now,
            )
        )
        return api_app.ChatResponse(answer=answer, messages=list(history))

    def list_chat_sessions(self, *, user_id: int, include_deleted: bool = False) -> list[Any]:
        store = self._ensure_user_session_store(int(user_id))
        sessions = []
        for record in store.values():
            if not include_deleted and record.get("deleted_at"):
                continue
            sessions.append(api_app.ChatSessionSummary(**record))
        sessions.sort(key=lambda item: item.updated_at, reverse=True)
        return sessions

    def create_chat_session(self, *, user_id: int, title: str = "", org_id: str | None = None) -> Any:
        store = self._ensure_user_session_store(int(user_id))
        now = self._now_iso()
        chat_id = f"chat-{uuid.uuid4().hex[:10]}"
        payload = {
            "chat_id": chat_id,
            "title": str(title or "").strip() or "New Chat",
            "created_at": now,
            "updated_at": now,
            "deleted_at": None,
            "message_count": 0,
            "preview": "",
        }
        store[chat_id] = payload
        return api_app.ChatSessionSummary(**payload)

    def rename_chat_session(self, *, user_id: int, chat_id: str, title: str) -> Any:
        store = self._ensure_user_session_store(int(user_id))
        key = str(chat_id).strip()
        if key not in store:
            raise HTTPException(status_code=404, detail="Chat session not found")
        store[key]["title"] = str(title).strip()
        store[key]["updated_at"] = self._now_iso()
        return api_app.ChatSessionSummary(**store[key])

    def delete_chat_session(self, *, user_id: int, chat_id: str) -> None:
        store = self._ensure_user_session_store(int(user_id))
        key = str(chat_id).strip()
        if key not in store:
            raise HTTPException(status_code=404, detail="Chat session not found")
        now = self._now_iso()
        store[key]["deleted_at"] = now
        store[key]["updated_at"] = now

    def restore_chat_session(self, *, user_id: int, chat_id: str) -> Any:
        store = self._ensure_user_session_store(int(user_id))
        key = str(chat_id).strip()
        if key not in store:
            raise HTTPException(status_code=404, detail="Chat session not found")
        store[key]["deleted_at"] = None
        store[key]["updated_at"] = self._now_iso()
        return api_app.ChatSessionSummary(**store[key])

    def purge_chat_session(self, *, user_id: int, chat_id: str) -> None:
        store = self._ensure_user_session_store(int(user_id))
        key = str(chat_id).strip()
        record = store.get(key)
        if record is None:
            raise HTTPException(status_code=404, detail="Chat session not found")
        if not record.get("deleted_at"):
            raise HTTPException(status_code=409, detail="Chat session must be in trash before purge")
        del store[key]

    def purge_all_trashed_chats(self, *, user_id: int) -> int:
        store = self._ensure_user_session_store(int(user_id))
        to_remove = [chat_id for chat_id, record in store.items() if record.get("deleted_at")]
        for chat_id in to_remove:
            del store[chat_id]
        return len(to_remove)


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, _FakeApiService]:
    fake = _FakeApiService()
    admin_id = fake.register_user(
        api_app.RegisterRequest(
            email="admin@acme.test",
            password="AdminPass123",
            full_name="Admin User",
            title="Administrator",
            profile_bio="Initial system admin",
        )
    )
    fake.roles[("acme", admin_id)] = "admin"

    monkeypatch.setattr(api_app, "service", fake)
    test_client = TestClient(api_app.app)
    return test_client, fake


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_auth_endpoints_register_login_and_link_telegram(client: tuple[TestClient, _FakeApiService]) -> None:
    test_client, fake = client

    register_response = test_client.post(
        "/api/v1/auth/register",
        json={
            "email": "user1@acme.test",
            "password": "StrongPass123",
            "full_name": "User One",
            "title": "PM",
            "profile_bio": "Product owner",
        },
    )
    assert register_response.status_code == 200
    user_id = int(register_response.json()["user_id"])

    login_response = test_client.post(
        "/api/v1/auth/login",
        json={
            "email": "user1@acme.test",
            "password": "StrongPass123",
        },
    )
    assert login_response.status_code == 200
    token = login_response.json()["access_token"]

    link_response = test_client.post(
        "/api/v1/users/me/telegram",
        json={"telegram_id": 123456789},
        headers=_auth_headers(token),
    )
    assert link_response.status_code == 200
    assert link_response.json() == {"status": "linked"}
    assert fake.users[user_id]["telegram_id"] == 123456789


def test_admin_endpoint_forbidden_for_non_admin(client: tuple[TestClient, _FakeApiService]) -> None:
    test_client, fake = client

    member_id = fake.register_user(
        api_app.RegisterRequest(
            email="member@acme.test",
            password="MemberPass123",
            full_name="Member",
            title="Analyst",
            profile_bio="Finance",
        )
    )
    fake.roles[("acme", member_id)] = "member"

    login_response = test_client.post(
        "/api/v1/auth/login",
        json={"email": "member@acme.test", "password": "MemberPass123"},
    )
    token = login_response.json()["access_token"]

    create_org_response = test_client.post(
        "/api/v1/admin/organizations",
        json={"org_id": "beta", "name": "Beta Org"},
        headers=_auth_headers(token),
    )
    assert create_org_response.status_code == 403
    assert "Admin access required" in create_org_response.text


def test_api_smoke_register_login_and_user_chat_flow(client: tuple[TestClient, _FakeApiService]) -> None:
    test_client, _ = client

    member_register = test_client.post(
        "/api/v1/auth/register",
        json={
            "email": "member2@acme.test",
            "password": "MemberPass123",
            "full_name": "Member Two",
            "title": "Finance Manager",
            "profile_bio": "Budget and risk",
        },
    )
    assert member_register.status_code == 200
    member_id = int(member_register.json()["user_id"])

    admin_login = test_client.post(
        "/api/v1/auth/login",
        json={"email": "admin@acme.test", "password": "AdminPass123"},
    )
    assert admin_login.status_code == 200
    admin_token = admin_login.json()["access_token"]

    create_org = test_client.post(
        "/api/v1/admin/organizations",
        json={"org_id": "acme", "name": "Acme Corp"},
        headers=_auth_headers(admin_token),
    )
    assert create_org.status_code == 200
    assert create_org.json() == {"status": "ok"}

    member_login = test_client.post(
        "/api/v1/auth/login",
        json={"email": "member2@acme.test", "password": "MemberPass123"},
    )
    assert member_login.status_code == 200
    member_token = member_login.json()["access_token"]

    chat_send = test_client.post(
        "/api/v1/chat/send",
        json={"message": "Budget risk for Q3"},
        headers=_auth_headers(member_token),
    )
    assert chat_send.status_code == 200
    assert chat_send.json()["answer"] == "ACK: Budget risk for Q3"

    chat_messages = test_client.get(
        "/api/v1/chat/messages",
        headers=_auth_headers(member_token),
    )
    assert chat_messages.status_code == 200
    payload = chat_messages.json()
    assert len(payload) == 2
    assert payload[0]["sender_type"] == "user"
    assert payload[1]["sender_type"] == "assistant"


def test_chat_send_is_available_for_new_user_scope(client: tuple[TestClient, _FakeApiService]) -> None:
    test_client, _ = client

    member_register = test_client.post(
        "/api/v1/auth/register",
        json={
            "email": "outsider@acme.test",
            "password": "OutsiderPass123",
            "full_name": "Outsider",
            "title": "Guest",
            "profile_bio": "Not in team",
        },
    )
    assert member_register.status_code == 200

    member_login = test_client.post(
        "/api/v1/auth/login",
        json={"email": "outsider@acme.test", "password": "OutsiderPass123"},
    )
    token = member_login.json()["access_token"]

    chat_send = test_client.post(
        "/api/v1/chat/send",
        json={"message": "Hello"},
        headers=_auth_headers(token),
    )
    assert chat_send.status_code == 200
    assert chat_send.json()["answer"] == "ACK: Hello"


def test_chat_trash_lifecycle_updates_session_lists_without_reload(client: tuple[TestClient, _FakeApiService]) -> None:
    test_client, _ = client

    member_register = test_client.post(
        "/api/v1/auth/register",
        json={
            "email": "trash-user@acme.test",
            "password": "TrashPass123",
            "full_name": "Trash User",
            "title": "Analyst",
            "profile_bio": "Trash checks",
        },
    )
    assert member_register.status_code == 200

    member_login = test_client.post(
        "/api/v1/auth/login",
        json={"email": "trash-user@acme.test", "password": "TrashPass123"},
    )
    assert member_login.status_code == 200
    token = member_login.json()["access_token"]
    headers = _auth_headers(token)

    first = test_client.post("/api/v1/chat/sessions", json={"title": "Chat A"}, headers=headers)
    second = test_client.post("/api/v1/chat/sessions", json={"title": "Chat B"}, headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    first_chat_id = first.json()["chat_id"]
    second_chat_id = second.json()["chat_id"]

    active_list = test_client.get("/api/v1/chat/sessions", headers=headers)
    assert active_list.status_code == 200
    active_payload = active_list.json()["sessions"]
    assert len(active_payload) == 2
    assert all(item["deleted_at"] is None for item in active_payload)

    delete_first = test_client.delete(f"/api/v1/chat/sessions/{first_chat_id}", headers=headers)
    assert delete_first.status_code == 200

    active_after_delete = test_client.get("/api/v1/chat/sessions", headers=headers)
    assert active_after_delete.status_code == 200
    active_after_delete_ids = {item["chat_id"] for item in active_after_delete.json()["sessions"]}
    assert active_after_delete_ids == {second_chat_id}

    trash_list = test_client.get("/api/v1/chat/sessions?include_deleted=true", headers=headers)
    assert trash_list.status_code == 200
    trash_payload = trash_list.json()["sessions"]
    assert len(trash_payload) == 2
    deleted_rows = [item for item in trash_payload if item["deleted_at"]]
    assert len(deleted_rows) == 1
    assert deleted_rows[0]["chat_id"] == first_chat_id

    purge_first = test_client.delete(f"/api/v1/chat/sessions/{first_chat_id}/purge", headers=headers)
    assert purge_first.status_code == 200

    after_purge = test_client.get("/api/v1/chat/sessions?include_deleted=true", headers=headers)
    assert after_purge.status_code == 200
    after_purge_ids = {item["chat_id"] for item in after_purge.json()["sessions"]}
    assert after_purge_ids == {second_chat_id}

    delete_second = test_client.delete(f"/api/v1/chat/sessions/{second_chat_id}", headers=headers)
    assert delete_second.status_code == 200

    purge_all = test_client.delete("/api/v1/chat/trash/purge", headers=headers)
    assert purge_all.status_code == 200
    assert purge_all.json()["purged"] == 1

    final_active = test_client.get("/api/v1/chat/sessions", headers=headers)
    assert final_active.status_code == 200
    assert final_active.json()["sessions"] == []

    final_all = test_client.get("/api/v1/chat/sessions?include_deleted=true", headers=headers)
    assert final_all.status_code == 200
    assert final_all.json()["sessions"] == []


def test_admin_dynamic_skill_endpoints_with_uploaded_markdown_file(client: tuple[TestClient, _FakeApiService]) -> None:
    test_client, _ = client

    admin_login = test_client.post(
        "/api/v1/auth/login",
        json={"email": "admin@acme.test", "password": "AdminPass123"},
    )
    assert admin_login.status_code == 200
    admin_token = admin_login.json()["access_token"]

    markdown_path = Path(__file__).with_name("marketing-pipeline-analyst.md")
    markdown = markdown_path.read_text(encoding="utf-8")

    convert = test_client.post(
        "/api/v1/admin/skills/convert-claude",
        json={
            "markdown": markdown,
            "skill_name": "pipeline_analyst",
            "overwrite": True,
        },
        headers=_auth_headers(admin_token),
    )
    assert convert.status_code == 200
    assert convert.json()["skill_name"] == "pipeline_analyst"

    listed = test_client.get(
        "/api/v1/admin/skills",
        headers=_auth_headers(admin_token),
    )
    assert listed.status_code == 200
    tool_names = [item["tool_name"] for item in listed.json()["skills"]]
    assert "pipeline_analyst" in tool_names

    deleted = test_client.delete(
        "/api/v1/admin/skills/pipeline_analyst",
        headers=_auth_headers(admin_token),
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True


def test_admin_dynamic_skill_multipart_bulk_and_dry_run(client: tuple[TestClient, _FakeApiService]) -> None:
    test_client, _ = client

    admin_login = test_client.post(
        "/api/v1/auth/login",
        json={"email": "admin@acme.test", "password": "AdminPass123"},
    )
    assert admin_login.status_code == 200
    admin_token = admin_login.json()["access_token"]

    markdown_path = Path(__file__).with_name("marketing-pipeline-analyst.md")
    markdown_bytes = markdown_path.read_bytes()

    dry_run = test_client.post(
        "/api/v1/admin/skills/convert-claude-files/dry-run",
        files=[("files", ("marketing-pipeline-analyst.md", markdown_bytes, "text/markdown"))],
        data={"skill_name_prefix": "batch"},
        headers=_auth_headers(admin_token),
    )
    assert dry_run.status_code == 200
    dry_payload = dry_run.json()
    assert dry_payload["total"] == 1
    assert dry_payload["valid"] == 1
    assert dry_payload["results"][0]["status"] == "ok"
    assert dry_payload["results"][0]["proposed_skill_name"] == "batch_marketing_pipeline_analyst"

    bulk = test_client.post(
        "/api/v1/admin/skills/convert-claude-files",
        files=[("files", ("marketing-pipeline-analyst.md", markdown_bytes, "text/markdown"))],
        data={"skill_name_prefix": "batch", "overwrite": "true"},
        headers=_auth_headers(admin_token),
    )
    assert bulk.status_code == 200
    bulk_payload = bulk.json()
    assert bulk_payload["created"] == 1
    assert bulk_payload["failed"] == 0
    assert bulk_payload["results"][0]["skill_name"] == "batch_marketing_pipeline_analyst"


def test_admin_org_membership_and_delete_user_flow(client: tuple[TestClient, _FakeApiService]) -> None:
    test_client, _ = client

    register_response = test_client.post(
        "/api/v1/auth/register",
        json={
            "email": "member-org@acme.test",
            "password": "MemberPass123",
            "full_name": "Member Org",
            "title": "Analyst",
            "profile_bio": "Org member",
        },
    )
    assert register_response.status_code == 200
    target_user_id = int(register_response.json()["user_id"])

    admin_login = test_client.post(
        "/api/v1/auth/login",
        json={"email": "admin@acme.test", "password": "AdminPass123"},
    )
    assert admin_login.status_code == 200
    admin_token = admin_login.json()["access_token"]

    create_org = test_client.post(
        "/api/v1/admin/organizations",
        json={"org_id": "beta", "name": "Beta Org"},
        headers=_auth_headers(admin_token),
    )
    assert create_org.status_code == 200
    assert create_org.json() == {"status": "ok"}

    list_orgs = test_client.get(
        "/api/v1/admin/organizations",
        headers=_auth_headers(admin_token),
    )
    assert list_orgs.status_code == 200
    listed_ids = {item["org_id"] for item in list_orgs.json()}
    assert "beta" in listed_ids

    bind_user = test_client.post(
        f"/api/v1/admin/users/{target_user_id}/organizations/add",
        json={"org_id": "beta", "role": "member"},
        headers=_auth_headers(admin_token),
    )
    assert bind_user.status_code == 200
    assert bind_user.json() == {"status": "ok", "created": True}

    bind_user_again = test_client.post(
        f"/api/v1/admin/users/{target_user_id}/organizations/add",
        json={"org_id": "beta", "role": "member"},
        headers=_auth_headers(admin_token),
    )
    assert bind_user_again.status_code == 200
    assert bind_user_again.json() == {"status": "ok", "created": False}

    unbind_user = test_client.delete(
        f"/api/v1/admin/users/{target_user_id}/organizations/beta",
        headers=_auth_headers(admin_token),
    )
    assert unbind_user.status_code == 200
    assert unbind_user.json() == {"status": "ok", "removed": True}

    delete_user = test_client.delete(
        f"/api/v1/admin/users/{target_user_id}",
        headers=_auth_headers(admin_token),
    )
    assert delete_user.status_code == 200
    assert delete_user.json() == {"status": "ok", "deleted": True, "user_id": target_user_id}

    deleted_user_login = test_client.post(
        "/api/v1/auth/login",
        json={"email": "member-org@acme.test", "password": "MemberPass123"},
    )
    assert deleted_user_login.status_code == 401

    delete_missing_user = test_client.delete(
        f"/api/v1/admin/users/{target_user_id}",
        headers=_auth_headers(admin_token),
    )
    assert delete_missing_user.status_code == 404


def test_send_group_chat_includes_org_employee_directory_context() -> None:
    service = api_app.ApiService.__new__(api_app.ApiService)

    captured: dict[str, Any] = {}

    class _DummyAgent:
        def __init__(self) -> None:
            self.registry = SimpleNamespace(tools={})

        def refresh_dynamic_tools(self) -> None:
            return None

        def run(self, message: str, history: list[dict[str, str]], *args: Any, **kwargs: Any) -> Any:
            captured["message"] = message
            captured["history"] = history
            return SimpleNamespace(answer="ok", messages=[])

    service.settings = SimpleNamespace(enable_dynamic_tools=False)
    service.rbac = SimpleNamespace(
        get_role=lambda **_: "member",
        resolve_allowed_skills=lambda **_: set(),
    )
    service.agent = _DummyAgent()
    service.long_term = None

    service._organization_people_context = lambda **_: "ORG_CTX"
    service._user_profile_text = lambda **_: "USER_CTX"
    service._recall_shared_memory = lambda **_: "MEM_CTX"
    service._load_group_history = lambda **_: [{"role": "user", "content": "previous message"}]
    service._append_group_message = lambda **_: None
    service._maybe_autotitle_chat_session = lambda **_: None
    service._read_group_messages = lambda **_: []

    response = service.send_group_chat(user_id=7, payload=api_app.ChatRequest(message="Who is on my team?"))

    assert response.answer == "ok"
    sent_history = captured["history"]
    assert [item["content"] for item in sent_history[:3]] == ["MEM_CTX", "USER_CTX", "ORG_CTX"]
    assert sent_history[3] == {"role": "user", "content": "previous message"}


def test_collect_user_org_ids_keeps_only_unique_non_empty_values() -> None:
    service = api_app.ApiService.__new__(api_app.ApiService)

    org_rows = [
        {"org_id": "acme"},
        {"org_id": "beta"},
        {"org_id": "acme"},
        {"org_id": ""},
        {"org_id": None},
    ]

    org_ids = service._collect_user_org_ids(org_rows=org_rows)

    assert org_ids == ["acme", "beta"]


def test_organization_people_context_filters_to_requester_orgs() -> None:
    service = api_app.ApiService.__new__(api_app.ApiService)

    class _FakeResult:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self._rows = rows

        def mappings(self) -> "_FakeResult":
            return self

        def all(self) -> list[dict[str, Any]]:
            return self._rows

    class _FakeConn:
        def __init__(self) -> None:
            self._calls = 0

        def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _FakeResult:
            self._calls += 1
            query_params = params or {}

            if self._calls == 1:
                # Memberships of requesting user: only acme.
                assert int(query_params.get("user_id") or 0) == 10
                return _FakeResult([{"org_id": "acme"}])

            if self._calls == 2:
                # Organization names lookup must be restricted to requester orgs.
                assert query_params.get("org_ids") == ["acme"]
                return _FakeResult([{"org_id": "acme", "name": "Acme Corp"}])

            if self._calls == 3:
                # Members query must also be restricted to requester orgs.
                assert query_params.get("org_ids") == ["acme"]
                return _FakeResult(
                    [
                        {
                            "org_id": "acme",
                            "user_id": 10,
                            "email": "requester@acme.test",
                            "full_name": "Requester",
                            "title": "Manager",
                            "profile_bio": "Leads product",
                        },
                        {
                            "org_id": "acme",
                            "user_id": 11,
                            "email": "colleague@acme.test",
                            "full_name": "Colleague",
                            "title": "Analyst",
                            "profile_bio": "Owns reporting",
                        },
                    ]
                )

            return _FakeResult([])

    class _FakeBegin:
        def __enter__(self) -> _FakeConn:
            return _FakeConn()

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            return False

    service.engine = SimpleNamespace(begin=lambda: _FakeBegin())
    service._table_exists = lambda table_name: table_name in {"org_memberships", "organizations"}

    context = service._organization_people_context(user_id=10)

    assert "Org acme (Acme Corp):" in context
    assert "Requester (you); title: Manager" in context
    assert "Colleague; title: Analyst" in context
    assert "Org beta" not in context
