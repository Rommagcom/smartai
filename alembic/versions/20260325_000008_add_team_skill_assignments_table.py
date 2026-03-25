"""add team skill assignments table

Revision ID: 20260325_000008
Revises: 20260320_000007
Create Date: 2026-03-25 00:00:08
"""

from __future__ import annotations

from alembic import op


revision = "20260325_000008"
down_revision = "20260320_000007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS team_skill_assignments (
            org_id TEXT NOT NULL,
            team_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            assigned_by BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
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


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_team_skill_assignments_org_team")
    op.execute("DROP TABLE IF EXISTS team_skill_assignments")
