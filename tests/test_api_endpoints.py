from __future__ import annotations

import os
import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
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

        self.orgs: dict[str, str] = {}
        self.teams: dict[tuple[str, str], str] = {}
        self.team_members: set[tuple[str, str, int]] = set()
        self.roles: dict[tuple[str, int], str] = {}
        self.skills: dict[tuple[str, int], set[str]] = {}
        self.messages: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.dynamic_skills: dict[str, dict[str, str]] = {}

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
        self.orgs[payload.org_id] = payload.name

    def create_team(self, *, actor_user_id: int, payload: Any) -> None:
        self._ensure_admin(actor_user_id)
        if payload.org_id not in self.orgs:
            raise HTTPException(status_code=404, detail="Organization does not exist")
        self.teams[(payload.org_id, payload.team_id)] = payload.name

    def add_to_team(self, *, actor_user_id: int, payload: Any) -> None:
        self._ensure_admin(actor_user_id)
        team_key = (payload.org_id, payload.team_id)
        if team_key not in self.teams:
            raise HTTPException(status_code=404, detail="Team does not exist")
        self.team_members.add((payload.org_id, payload.team_id, int(payload.user_id)))

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

    def _assert_membership(self, *, org_id: str, team_id: str, user_id: int) -> None:
        if (org_id, team_id, int(user_id)) not in self.team_members:
            raise HTTPException(status_code=403, detail="User is not a member of the target team")

    def _read_group_messages(self, *, org_id: str, team_id: str, limit: int = 30) -> list[dict[str, Any]]:
        history = self.messages.get((org_id, team_id), [])
        return history[-max(1, int(limit)) :]

    def send_group_chat(self, *, user_id: int, payload: Any) -> Any:
        self._assert_membership(org_id=payload.org_id, team_id=payload.team_id, user_id=user_id)

        history = self.messages.setdefault((payload.org_id, payload.team_id), [])
        now = datetime.now(UTC).isoformat()
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


def test_api_smoke_register_login_admin_team_chat_flow(client: tuple[TestClient, _FakeApiService]) -> None:
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

    create_team = test_client.post(
        "/api/v1/admin/teams",
        json={"org_id": "acme", "team_id": "finance", "name": "Finance Team"},
        headers=_auth_headers(admin_token),
    )
    assert create_team.status_code == 200

    add_member = test_client.post(
        "/api/v1/admin/teams/members",
        json={"org_id": "acme", "team_id": "finance", "user_id": member_id},
        headers=_auth_headers(admin_token),
    )
    assert add_member.status_code == 200

    set_role = test_client.post(
        f"/api/v1/admin/users/{member_id}/role",
        json={"org_id": "acme", "role": "manager"},
        headers=_auth_headers(admin_token),
    )
    assert set_role.status_code == 200

    grant_skill = test_client.post(
        f"/api/v1/admin/users/{member_id}/skills/grant",
        json={"org_id": "acme", "tool_name": "reminder_scheduler"},
        headers=_auth_headers(admin_token),
    )
    assert grant_skill.status_code == 200

    list_skills = test_client.get(
        f"/api/v1/admin/users/{member_id}/skills",
        params={"org_id": "acme"},
        headers=_auth_headers(admin_token),
    )
    assert list_skills.status_code == 200
    assert list_skills.json()["skills"] == ["reminder_scheduler"]

    member_login = test_client.post(
        "/api/v1/auth/login",
        json={"email": "member2@acme.test", "password": "MemberPass123"},
    )
    assert member_login.status_code == 200
    member_token = member_login.json()["access_token"]

    chat_send = test_client.post(
        "/api/v1/chat/send",
        json={"org_id": "acme", "team_id": "finance", "message": "Budget risk for Q3"},
        headers=_auth_headers(member_token),
    )
    assert chat_send.status_code == 200
    assert chat_send.json()["answer"] == "ACK: Budget risk for Q3"

    chat_messages = test_client.get(
        "/api/v1/chat/messages",
        params={"org_id": "acme", "team_id": "finance"},
        headers=_auth_headers(member_token),
    )
    assert chat_messages.status_code == 200
    payload = chat_messages.json()
    assert len(payload) == 2
    assert payload[0]["sender_type"] == "user"
    assert payload[1]["sender_type"] == "assistant"


def test_chat_send_forbidden_if_user_not_in_team(client: tuple[TestClient, _FakeApiService]) -> None:
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

    denied = test_client.post(
        "/api/v1/chat/send",
        json={"org_id": "acme", "team_id": "finance", "message": "Hello"},
        headers=_auth_headers(token),
    )
    assert denied.status_code == 403
    assert "not a member" in denied.text


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
