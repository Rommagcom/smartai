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
        self._next_bot_id = 1
        self._next_ledger_id = 1
        self.users: dict[int, dict[str, Any]] = {}
        self.users_by_email: dict[str, int] = {}
        self.tokens: dict[str, int] = {}

        self.roles: dict[tuple[str, int], str] = {}
        self.skills: dict[tuple[str, int], set[str]] = {}
        self.messages: dict[int, list[dict[str, Any]]] = {}
        self.dynamic_skills: dict[str, dict[str, str]] = {}
        self.byob_bots: dict[str, list[dict[str, Any]]] = {}
        self.org_credits: dict[str, int] = {}
        self.org_ledger: dict[str, list[dict[str, Any]]] = {}
        self.org_credit_policies: dict[str, dict[str, Any]] = {}

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

    def register_byob_bot(self, *, actor_user_id: int, org_id: str, payload: Any) -> Any:
        self._ensure_admin(actor_user_id)
        now = datetime.now(UTC).isoformat()
        record = {
            "id": self._next_bot_id,
            "org_id": org_id,
            "provider": payload.provider,
            "bot_name": payload.bot_name,
            "external_bot_id": payload.external_bot_id,
            "token_hint": "***token",
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        }
        self._next_bot_id += 1
        self.byob_bots.setdefault(org_id, []).append(record)
        return api_app.ByobBotSummary(**record)

    def list_byob_bots(self, *, actor_user_id: int, org_id: str, provider: str | None = None) -> list[Any]:
        self._ensure_admin(actor_user_id)
        items = self.byob_bots.get(org_id, [])
        if provider:
            items = [item for item in items if item["provider"] == provider]
        return [api_app.ByobBotSummary(**item) for item in items]

    def update_byob_bot_status(self, *, actor_user_id: int, org_id: str, bot_id: int, payload: Any) -> Any:
        self._ensure_admin(actor_user_id)
        for item in self.byob_bots.get(org_id, []):
            if int(item["id"]) == int(bot_id):
                item["is_active"] = bool(payload.is_active)
                item["updated_at"] = datetime.now(UTC).isoformat()
                return api_app.ByobBotSummary(**item)
        raise HTTPException(status_code=404, detail="Bot connection not found")

    def get_org_credit_balance(self, *, actor_user_id: int, org_id: str) -> Any:
        self._ensure_admin(actor_user_id)
        return api_app.OrgCreditBalanceResponse(org_id=org_id, balance=int(self.org_credits.get(org_id, 0)))

    def _apply_credits(self, *, org_id: str, delta: int, actor_user_id: int, payload: Any) -> Any:
        balance = int(self.org_credits.get(org_id, 0)) + int(delta)
        if balance < 0:
            raise HTTPException(status_code=409, detail="Insufficient credits")
        self.org_credits[org_id] = balance
        self.org_ledger.setdefault(org_id, []).append(
            {
                "id": self._next_ledger_id,
                "delta": int(delta),
                "balance_after": balance,
                "reason": payload.reason,
                "actor_user_id": int(actor_user_id),
                "reference_type": payload.reference_type,
                "reference_id": payload.reference_id,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        self._next_ledger_id += 1
        return api_app.OrgCreditBalanceResponse(org_id=org_id, balance=balance)

    def top_up_org_credits(self, *, actor_user_id: int, org_id: str, payload: Any) -> Any:
        self._ensure_admin(actor_user_id)
        return self._apply_credits(org_id=org_id, delta=int(payload.amount), actor_user_id=actor_user_id, payload=payload)

    def debit_org_credits(self, *, actor_user_id: int, org_id: str, payload: Any) -> Any:
        self._ensure_admin(actor_user_id)
        policy = self.org_credit_policies.get(org_id, {})
        daily_limit = policy.get("daily_limit")
        monthly_limit = policy.get("monthly_limit")
        spent = sum(-int(item["delta"]) for item in self.org_ledger.get(org_id, []) if int(item["delta"]) < 0)
        next_spent = spent + int(payload.amount)
        if daily_limit is not None and next_spent > int(daily_limit):
            raise HTTPException(status_code=409, detail="Daily credit limit exceeded")
        if monthly_limit is not None and next_spent > int(monthly_limit):
            raise HTTPException(status_code=409, detail="Monthly credit limit exceeded")
        return self._apply_credits(org_id=org_id, delta=-int(payload.amount), actor_user_id=actor_user_id, payload=payload)

    def list_org_credit_ledger(self, *, actor_user_id: int, org_id: str, limit: int = 50) -> Any:
        self._ensure_admin(actor_user_id)
        items = self.org_ledger.get(org_id, [])
        modeled = [api_app.CreditLedgerItem(**item) for item in items[-max(1, int(limit)) :]][::-1]
        return api_app.CreditLedgerResponse(org_id=org_id, items=modeled)

    def get_org_credit_policy(self, *, actor_user_id: int, org_id: str) -> Any:
        self._ensure_admin(actor_user_id)
        policy = self.org_credit_policies.get(org_id, {})
        return api_app.CreditPolicyResponse(
            org_id=org_id,
            daily_limit=policy.get("daily_limit"),
            monthly_limit=policy.get("monthly_limit"),
            low_balance_threshold=int(policy.get("low_balance_threshold") or 0),
        )

    def set_org_credit_policy(self, *, actor_user_id: int, org_id: str, payload: Any) -> Any:
        self._ensure_admin(actor_user_id)
        self.org_credit_policies[org_id] = {
            "daily_limit": payload.daily_limit,
            "monthly_limit": payload.monthly_limit,
            "low_balance_threshold": int(payload.low_balance_threshold),
        }
        return self.get_org_credit_policy(actor_user_id=actor_user_id, org_id=org_id)

    def process_telegram_webhook(
        self,
        *,
        org_id: str,
        bot_id: int,
        webhook_secret: str,
        client_ip: str | None,
        update: Any,
    ) -> dict[str, Any]:
        _ = (org_id, bot_id, webhook_secret, client_ip, update)
        return {"method": "sendMessage", "chat_id": 1, "text": "ACK"}

    def get_byob_queue_health(self, *, actor_user_id: int, org_id: str) -> Any:
        self._ensure_admin(actor_user_id)
        return api_app.ByobQueueHealthResponse(
            org_id=org_id,
            pending=0,
            retry=0,
            failed=0,
            sent_last_24h=0,
            oldest_due_at=None,
        )

    def process_byob_delivery_backlog(self) -> int:
        return 0

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

    def _read_group_messages(self, *, user_id: int, limit: int = 30) -> list[dict[str, Any]]:
        history = self.messages.get(int(user_id), [])
        return history[-max(1, int(limit)) :]

    def send_group_chat(self, *, user_id: int, payload: Any) -> Any:
        history = self.messages.setdefault(int(user_id), [])
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


def test_admin_can_set_role_and_manage_skills(client: tuple[TestClient, _FakeApiService]) -> None:
    test_client, _ = client

    member_register = test_client.post(
        "/api/v1/auth/register",
        json={
            "email": "member3@acme.test",
            "password": "MemberPass123",
            "full_name": "Member Three",
            "title": "Analyst",
            "profile_bio": "Operations",
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

    set_role_response = test_client.post(
        f"/api/v1/admin/users/{member_id}/role",
        json={"org_id": "acme", "role": "member"},
        headers=_auth_headers(admin_token),
    )
    assert set_role_response.status_code == 200

    grant_response = test_client.post(
        f"/api/v1/admin/users/{member_id}/skills/grant",
        json={"org_id": "acme", "tool_name": "seo_specialist"},
        headers=_auth_headers(admin_token),
    )
    assert grant_response.status_code == 200

    list_response = test_client.get(
        f"/api/v1/admin/users/{member_id}/skills",
        params={"org_id": "acme"},
        headers=_auth_headers(admin_token),
    )
    assert list_response.status_code == 200
    assert "seo_specialist" in list_response.json()["skills"]

    revoke_response = test_client.post(
        f"/api/v1/admin/users/{member_id}/skills/revoke",
        json={"org_id": "acme", "tool_name": "seo_specialist"},
        headers=_auth_headers(admin_token),
    )
    assert revoke_response.status_code == 200
    assert revoke_response.json()["removed"] is True


def test_admin_can_manage_byob_bot_and_credits(client: tuple[TestClient, _FakeApiService]) -> None:
    test_client, _ = client

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

    register_bot = test_client.post(
        "/api/v1/orgs/acme/bots",
        json={
            "provider": "telegram",
            "bot_name": "Acme Sales Bot",
            "bot_token": "123456:abcdef-token-value",
            "external_bot_id": "987654321",
        },
        headers=_auth_headers(admin_token),
    )
    assert register_bot.status_code == 200
    bot_id = int(register_bot.json()["id"])

    deactivate_bot = test_client.patch(
        f"/api/v1/orgs/acme/bots/{bot_id}",
        json={"is_active": False},
        headers=_auth_headers(admin_token),
    )
    assert deactivate_bot.status_code == 200
    assert deactivate_bot.json()["is_active"] is False

    topup = test_client.post(
        "/api/v1/orgs/acme/credits/topup",
        json={"amount": 1000, "reason": "Initial package", "reference_type": "invoice", "reference_id": "INV-1"},
        headers=_auth_headers(admin_token),
    )
    assert topup.status_code == 200
    assert topup.json()["balance"] == 1000

    debit = test_client.post(
        "/api/v1/orgs/acme/credits/debit",
        json={"amount": 250, "reason": "LLM usage", "reference_type": "usage", "reference_id": "req-1"},
        headers=_auth_headers(admin_token),
    )
    assert debit.status_code == 200
    assert debit.json()["balance"] == 750

    ledger = test_client.get(
        "/api/v1/orgs/acme/credits/ledger",
        headers=_auth_headers(admin_token),
    )
    assert ledger.status_code == 200
    assert len(ledger.json()["items"]) == 2


def test_credit_policy_blocks_debit_when_limit_exceeded(client: tuple[TestClient, _FakeApiService]) -> None:
    test_client, _ = client

    admin_login = test_client.post(
        "/api/v1/auth/login",
        json={"email": "admin@acme.test", "password": "AdminPass123"},
    )
    admin_token = admin_login.json()["access_token"]

    test_client.post(
        "/api/v1/admin/organizations",
        json={"org_id": "acme", "name": "Acme Corp"},
        headers=_auth_headers(admin_token),
    )

    set_policy = test_client.put(
        "/api/v1/orgs/acme/credits/policy",
        json={"daily_limit": 100, "monthly_limit": 1000, "low_balance_threshold": 10},
        headers=_auth_headers(admin_token),
    )
    assert set_policy.status_code == 200
    assert set_policy.json()["daily_limit"] == 100

    topup = test_client.post(
        "/api/v1/orgs/acme/credits/topup",
        json={"amount": 500, "reason": "Fund", "reference_type": "invoice", "reference_id": "INV-2"},
        headers=_auth_headers(admin_token),
    )
    assert topup.status_code == 200

    debit_too_much = test_client.post(
        "/api/v1/orgs/acme/credits/debit",
        json={"amount": 150, "reason": "Usage", "reference_type": "usage", "reference_id": "REQ-2"},
        headers=_auth_headers(admin_token),
    )
    assert debit_too_much.status_code == 409


def test_byob_telegram_webhook_endpoint(client: tuple[TestClient, _FakeApiService]) -> None:
    test_client, _ = client

    response = test_client.post(
        "/api/v1/byob/telegram/acme/1/webhook",
        json={"update_id": 1, "message": {"chat": {"id": 1}, "from": {"id": 2}, "text": "hello"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
    )
    assert response.status_code == 200
    assert response.json().get("method") == "sendMessage"


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
