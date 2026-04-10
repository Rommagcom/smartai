from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

Role = Literal["admin", "manager", "member"]
ORG_ID_EMPTY_TEXT = "org_id must not be empty"
DEFAULT_SYSTEM_SKILLS: set[str] = {"utc_system_time"}


@dataclass(slots=True)
class TenantContext:
    org_id: str
    team_id: str
    user_id: int
    role: Role
    chat_id: int


class RbacStore:
    def __init__(self, db_url: str | None = None) -> None:
        resolved = self._resolve_database_url(db_url)
        if not resolved:
            raise RuntimeError("RBAC database URL is not configured")
        self.engine: Engine = create_engine(resolved, future=True, pool_pre_ping=True)

    @staticmethod
    def _resolve_database_url(explicit: str | None = None) -> str:
        value = (explicit or os.getenv("REMINDER_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
        if value.startswith("postgres://"):
            return "postgresql+psycopg://" + value.removeprefix("postgres://")
        if value.startswith("postgresql://"):
            return "postgresql+psycopg://" + value.removeprefix("postgresql://")
        return value

    def upsert_user(self, *, org_id: str, user_id: int, role: Role) -> None:
        now = datetime.now(UTC)
        stmt = text(
            """
            INSERT INTO app_users (org_id, user_id, role, created_at, updated_at)
            VALUES (:org_id, :user_id, :role, :now, :now)
            ON CONFLICT (org_id, user_id)
            DO UPDATE SET role = EXCLUDED.role, updated_at = EXCLUDED.updated_at
            """
        )
        with self.engine.begin() as conn:
            conn.execute(
                stmt,
                {
                    "org_id": org_id,
                    "user_id": int(user_id),
                    "role": role,
                    "now": now,
                },
            )

    def ensure_organization(self, *, org_id: str, name: str) -> None:
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO organizations (org_id, name, created_at, updated_at)
                    VALUES (:org_id, :name, :created_at, :updated_at)
                    ON CONFLICT (org_id)
                    DO UPDATE SET name = EXCLUDED.name, updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "org_id": org_id,
                    "name": name,
                    "created_at": now,
                    "updated_at": now,
                },
            )

    def create_organization(self, *, actor_user_id: int, org_id: str, name: str) -> None:
        normalized_org = org_id.strip()
        if not normalized_org:
            raise ValueError(ORG_ID_EMPTY_TEXT)
        normalized_name = name.strip() or normalized_org
        self.ensure_organization(org_id=normalized_org, name=normalized_name)
        self.audit(
            org_id=normalized_org,
            team_id="",
            actor_user_id=actor_user_id,
            action="rbac.create_organization",
            target_type="organization",
            target_id=normalized_org,
            details={"name": normalized_name},
        )

    def get_role(self, *, org_id: str, user_id: int, fallback_role: Role = "member") -> Role:
        with self.engine.begin() as conn:
            row = conn.execute(
                text("SELECT role FROM app_users WHERE org_id = :org_id AND user_id = :user_id LIMIT 1"),
                {
                    "org_id": org_id,
                    "user_id": int(user_id),
                },
            ).mappings().first()
        if row is None:
            return fallback_role
        role = str(row.get("role") or fallback_role).strip().lower()
        if role in {"admin", "manager", "member"}:
            return role  # type: ignore[return-value]
        return fallback_role

    def set_role(self, *, org_id: str, actor_user_id: int, target_user_id: int, role: Role) -> None:
        self.upsert_user(org_id=org_id, user_id=target_user_id, role=role)
        self.audit(
            org_id=org_id,
            team_id="",
            actor_user_id=actor_user_id,
            action="rbac.set_role",
            target_type="user",
            target_id=str(target_user_id),
            details={"role": role},
        )

    def assign_skill(self, *, org_id: str, actor_user_id: int, target_user_id: int, tool_name: str) -> None:
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO user_skill_assignments (org_id, user_id, tool_name, assigned_by, created_at)
                    VALUES (:org_id, :user_id, :tool_name, :assigned_by, :created_at)
                    ON CONFLICT (org_id, user_id, tool_name) DO NOTHING
                    """
                ),
                {
                    "org_id": org_id,
                    "user_id": int(target_user_id),
                    "tool_name": tool_name,
                    "assigned_by": int(actor_user_id),
                    "created_at": now,
                },
            )
        self.audit(
            org_id=org_id,
            team_id="",
            actor_user_id=actor_user_id,
            action="rbac.assign_skill",
            target_type="skill",
            target_id=tool_name,
            details={"target_user_id": target_user_id},
        )

    def revoke_skill(self, *, org_id: str, actor_user_id: int, target_user_id: int, tool_name: str) -> bool:
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    DELETE FROM user_skill_assignments
                    WHERE org_id = :org_id AND user_id = :user_id AND tool_name = :tool_name
                    """
                ),
                {
                    "org_id": org_id,
                    "user_id": int(target_user_id),
                    "tool_name": tool_name,
                },
            )
        removed = int(result.rowcount or 0) > 0
        self.audit(
            org_id=org_id,
            team_id="",
            actor_user_id=actor_user_id,
            action="rbac.revoke_skill",
            target_type="skill",
            target_id=tool_name,
            details={"target_user_id": target_user_id, "removed": removed},
        )
        return removed

    def list_user_skills(self, *, org_id: str, user_id: int) -> list[str]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT tool_name
                    FROM user_skill_assignments
                    WHERE org_id = :org_id AND user_id = :user_id
                    ORDER BY tool_name ASC
                    """
                ),
                {
                    "org_id": org_id,
                    "user_id": int(user_id),
                },
            ).mappings().all()
        return [str(row.get("tool_name") or "").strip() for row in rows if str(row.get("tool_name") or "").strip()]

    def resolve_allowed_skills(
        self,
        *,
        org_id: str,
        user_id: int,
        role: Role,
        all_dynamic_tools: set[str],
    ) -> set[str]:
        default_system = set(all_dynamic_tools).intersection(DEFAULT_SYSTEM_SKILLS)
        if role == "admin":
            return set(all_dynamic_tools)

        assigned = set(self.list_user_skills(org_id=org_id, user_id=user_id))
        if role == "manager":
            return assigned.intersection(all_dynamic_tools).union(default_system)
        return assigned.intersection(all_dynamic_tools).union(default_system)

    def audit(
        self,
        *,
        org_id: str,
        team_id: str,
        actor_user_id: int,
        action: str,
        target_type: str,
        target_id: str,
        details: dict[str, object] | None = None,
    ) -> None:
        payload = details or {}
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO audit_events (
                        org_id, team_id, actor_user_id, action, target_type, target_id, details_json, created_at
                    )
                    VALUES (
                        :org_id, :team_id, :actor_user_id, :action, :target_type, :target_id, :details_json, :created_at
                    )
                    """
                ),
                {
                    "org_id": org_id,
                    "team_id": team_id,
                    "actor_user_id": int(actor_user_id),
                    "action": action,
                    "target_type": target_type,
                    "target_id": target_id,
                    "details_json": json_dumps(payload),
                    "created_at": datetime.now(UTC),
                },
            )


def json_dumps(value: dict[str, object]) -> str:
    import json

    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
