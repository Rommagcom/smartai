"""drop team artifacts after user scope migration

Revision ID: 20260330_000012
Revises: 20260330_000011
Create Date: 2026-03-30 00:00:12
"""

from __future__ import annotations

from alembic import op


revision = "20260330_000012"
down_revision = "20260330_000011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_team_skill_assignments_org_team")
    op.execute("DROP TABLE IF EXISTS team_skill_assignments")
    op.execute("DROP TABLE IF EXISTS team_members")
    op.execute("DROP TABLE IF EXISTS teams")


def downgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS teams (
            org_id TEXT NOT NULL,
            team_id TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (org_id, team_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS team_members (
            org_id TEXT NOT NULL,
            team_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (org_id, team_id, user_id),
            FOREIGN KEY (org_id, team_id) REFERENCES teams(org_id, team_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS team_skill_assignments (
            org_id TEXT NOT NULL,
            team_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            assigned_by INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (org_id, team_id, tool_name),
            FOREIGN KEY (org_id, team_id) REFERENCES teams(org_id, team_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_team_skill_assignments_org_team
        ON team_skill_assignments (org_id, team_id)
        """
    )
