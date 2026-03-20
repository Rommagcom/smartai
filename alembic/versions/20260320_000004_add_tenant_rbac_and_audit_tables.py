"""add tenant isolation, rbac and audit tables

Revision ID: 20260320_000004
Revises: 20260320_000003
Create Date: 2026-03-20 00:00:04
"""

from __future__ import annotations

from alembic import op


revision = "20260320_000004"
down_revision = "20260320_000003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE reminders
        ADD COLUMN IF NOT EXISTS org_id TEXT NOT NULL DEFAULT 'default-org',
        ADD COLUMN IF NOT EXISTS team_id TEXT NOT NULL DEFAULT 'chat',
        ADD COLUMN IF NOT EXISTS user_id BIGINT NOT NULL DEFAULT 0
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_reminders_org_team_user_next_run
        ON reminders (org_id, team_id, user_id, next_run_at)
        """
    )

    op.execute(
        """
        ALTER TABLE long_term_memories
        ADD COLUMN IF NOT EXISTS org_id TEXT NOT NULL DEFAULT 'default-org',
        ADD COLUMN IF NOT EXISTS team_id TEXT NOT NULL DEFAULT 'chat',
        ADD COLUMN IF NOT EXISTS user_id BIGINT NOT NULL DEFAULT 0
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_long_term_memories_org_team_user_created
        ON long_term_memories (org_id, team_id, user_id, created_at DESC)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_users (
            org_id TEXT NOT NULL,
            user_id BIGINT NOT NULL,
            role VARCHAR(16) NOT NULL DEFAULT 'member',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (org_id, user_id)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS teams (
            org_id TEXT NOT NULL,
            team_id TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (org_id, team_id)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS team_members (
            org_id TEXT NOT NULL,
            team_id TEXT NOT NULL,
            user_id BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (org_id, team_id, user_id),
            FOREIGN KEY (org_id, team_id) REFERENCES teams(org_id, team_id) ON DELETE CASCADE
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_skill_assignments (
            org_id TEXT NOT NULL,
            user_id BIGINT NOT NULL,
            tool_name TEXT NOT NULL,
            assigned_by BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (org_id, user_id, tool_name)
        )
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_user_skill_assignments_org_user
        ON user_skill_assignments (org_id, user_id)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            id BIGSERIAL PRIMARY KEY,
            org_id TEXT NOT NULL,
            team_id TEXT NOT NULL,
            actor_user_id BIGINT NOT NULL,
            action TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_audit_events_org_team_created
        ON audit_events (org_id, team_id, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_audit_events_org_team_created")
    op.execute("DROP TABLE IF EXISTS audit_events")

    op.execute("DROP INDEX IF EXISTS ix_user_skill_assignments_org_user")
    op.execute("DROP TABLE IF EXISTS user_skill_assignments")

    op.execute("DROP TABLE IF EXISTS team_members")
    op.execute("DROP TABLE IF EXISTS teams")
    op.execute("DROP TABLE IF EXISTS app_users")

    op.execute("DROP INDEX IF EXISTS ix_long_term_memories_org_team_user_created")
    op.execute("ALTER TABLE long_term_memories DROP COLUMN IF EXISTS user_id")
    op.execute("ALTER TABLE long_term_memories DROP COLUMN IF EXISTS team_id")
    op.execute("ALTER TABLE long_term_memories DROP COLUMN IF EXISTS org_id")

    op.execute("DROP INDEX IF EXISTS ix_reminders_org_team_user_next_run")
    op.execute("ALTER TABLE reminders DROP COLUMN IF EXISTS user_id")
    op.execute("ALTER TABLE reminders DROP COLUMN IF EXISTS team_id")
    op.execute("ALTER TABLE reminders DROP COLUMN IF EXISTS org_id")
